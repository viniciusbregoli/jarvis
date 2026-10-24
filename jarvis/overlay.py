"""The HAL bar: a floating panel at the bottom of the screen showing who's talking, what Jarvis is doing, and its
background jobs. Same idea as Screamr's flow bar (never takes focus, clicks pass through), in HAL's colors.

Everything here runs on the main thread: other threads only set plain attributes (`levels`, `activity`, `talking`)
and `tick()` redraws from them 30 times a second."""

import math
import time
from pathlib import Path

from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSShadow,
    NSView,
)
from Quartz import CGEventGetIntegerValueField, kCGEventSourceStateID, kCGEventSourceUnixProcessID

RED = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.13, 0.08, 1.0)
DIM_RED = NSColor.colorWithSRGBRed_green_blue_alpha_(0.75, 0.12, 0.08, 1.0)
WARM_WHITE = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.92, 0.82, 1.0)
GREEN = NSColor.colorWithSRGBRed_green_blue_alpha_(0.45, 0.85, 0.45, 1.0)

BARS = 9
FULL_SIZE = (420, 56)
JOB_ROW = 22
FINISHED_JOB_SECONDS = 6  # a finished chip stays this long, roughly until its result is announced
ESCAPE_KEY = 53
KEY_DOWN_MASK = 1 << 10

# What the activity line says while each tool runs.
ACTIVITY = {
    "open_app": lambda p: f"Abrindo {p.get('target', '')}",
    "media": lambda p: "Ajustando o som",
    "set_timer": lambda p: "Criando o timer",
    "add_reminder": lambda p: "Criando o lembrete",
    "read_clipboard": lambda p: "Lendo a área de transferência",
    "describe_screen": lambda p: "Olhando a tela",
    "lookup": lambda p: "Pesquisando",
    "front_document": lambda p: "Olhando o que está na tela",
    "undo_task": lambda p: "Desfazendo",
    "start_task": lambda p: "Abrindo um agente para editar" if p.get("kind") == "edit" else "Iniciando uma tarefa",
    "task_status": lambda p: "Verificando as tarefas",
    "cancel_task": lambda p: "Cancelando a tarefa",
    "reply_to_task": lambda p: "Continuando a tarefa",
}


def activity_label(tool: str, params: dict) -> str:
    return ACTIVITY.get(tool, lambda p: tool)(params)


def is_user_key(source_pid: int) -> bool:
    """Synthetic key events (osascript, CGEventPost, Codex's computer use) carry the posting process's pid; hardware
    key presses are believed to carry 0. Unconfirmed on a real key press, hence the log line in HALBar._key."""
    return source_pid == 0


def job_label(job, now: float) -> str:
    seconds = int((job.ended or now) - job.started)
    mark = {"running": "●", "done": "✓"}.get(job.status, "✕")
    task = job.task if len(job.task) <= 48 else job.task[:47] + "…"
    return f"{mark} {task} · {seconds // 60}:{seconds % 60:02d}"


def visible_jobs(jobs, now: float) -> list:
    return [j for j in jobs if j.status == "running" or (j.ended and now - j.ended < FINISHED_JOB_SECONDS)]


def text(string: str, size: float, color, weight: float = 0.0) -> NSAttributedString:
    attributes = {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
        NSForegroundColorAttributeName: color,
    }
    return NSAttributedString.alloc().initWithString_attributes_(string, attributes)


class HALView(NSView):
    def isFlipped(self):
        return True  # draw top-down

    def drawRect_(self, _rect):
        bar = getattr(self, "bar", None)
        if bar is None:
            return
        bounds = self.bounds()
        width, height = bounds.size.width, bounds.size.height
        t = time.monotonic()

        # Black panel with a red glow.
        panel = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(8, 8, width - 16, height - 16), 14, 14
        )
        glow = NSShadow.alloc().init()
        glow.setShadowColor_(RED.colorWithAlphaComponent_(0.55 + 0.35 * bar.voice))
        glow.setShadowBlurRadius_(10)
        glow.set()
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.02, 0.0, 0.0, 0.94).setFill()
        panel.fill()
        NSShadow.alloc().init().set()
        RED.colorWithAlphaComponent_(0.45).setStroke()
        panel.setLineWidth_(1)
        panel.stroke()

        # The eye, brighter and slightly bigger while Jarvis speaks.
        size = (30 if bar.talking else 18) + 4 * bar.voice
        row = FULL_SIZE[1] if bar.talking else height
        bar.eye.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(37 - size / 2, row / 2 - size / 2, size, size), NSMakeRect(0, 0, 0, 0), 2, 0.6 + 0.4 * bar.voice,
            True, None,
        )  # fmt: skip

        x = 64
        if bar.talking:
            mid = 8 + (FULL_SIZE[1] - 16) / 2
            for group, level, color in ((0, bar.mic, WARM_WHITE), (1, bar.voice, RED)):
                for i in range(BARS):
                    wave = 0.55 + 0.45 * math.sin(t * 9 + i * 0.8 + group * 2)
                    bar_height = 3 + 22 * level * wave
                    color.setFill()
                    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        NSMakeRect(x + i * 6, mid - bar_height / 2, 3, bar_height), 1.5, 1.5
                    ).fill()
                x += BARS * 6 + 12
            if bar.activity:
                dots = "." * (1 + int(t * 3) % 3)
                text(bar.activity + dots, 12, DIM_RED, 0.3).drawAtPoint_((x + 4, mid - 8))

        y = FULL_SIZE[1] - 8 if bar.talking else 16
        for job in bar.jobs_shown:
            color = {"running": WARM_WHITE, "done": GREEN}.get(job.status, RED)
            text(job_label(job, time.time()), 11.5, color, 0.2).drawAtPoint_((64, y))
            y += JOB_ROW


class HALBar:
    def __init__(self, jobs, eye_path: Path):
        self.jobs = jobs
        self.eye = NSImage.alloc().initWithContentsOfFile_(str(eye_path))
        # Set from other threads.
        self.levels = lambda: (0.0, 0.0)  # (you, Jarvis)
        self.activity = ""
        self.talking = False
        # Read by HALView while drawing.
        self.mic = self.voice = 0.0
        self.jobs_shown = []
        self.visible = False
        self.panel = self._make_panel()
        self.on_escape = None  # any Esc while talking
        self.on_user_escape = None  # Esc from the keyboard, not synthesized by a desktop job
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(KEY_DOWN_MASK, self._key)

    def _make_panel(self):
        frame = NSMakeRect(0, 0, *FULL_SIZE)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(frame, 0 | (1 << 7), 2, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)  # the view draws its own red glow
        panel.setLevel_(25)  # NSStatusWindowLevel
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_((1 << 0) | (1 << 8) | (1 << 4) | (1 << 6))  # all spaces, fullscreen, stationary
        self.view = HALView.alloc().initWithFrame_(frame)
        self.view.bar = self
        panel.setContentView_(self.view)
        return panel

    def _key(self, event):
        if event.keyCode() != ESCAPE_KEY:
            return
        cg_event = event.CGEvent()
        pid = CGEventGetIntegerValueField(cg_event, kCGEventSourceUnixProcessID)
        state = CGEventGetIntegerValueField(cg_event, kCGEventSourceStateID)
        print(f"esc: source pid {pid}, state id {state}")
        # A desktop job presses Esc in apps too: only a real key press may cancel jobs, or they would cancel themselves.
        if is_user_key(pid) and self.on_user_escape:
            self.on_user_escape()
        if self.talking and self.on_escape:
            self.on_escape()

    def tick(self):
        now = time.time()
        self.jobs_shown = visible_jobs(self.jobs.all.values(), now)
        should_show = self.talking or bool(self.jobs_shown)
        if not should_show:
            if self.visible:
                self.panel.orderOut_(None)
                self.visible = False
            return
        self.mic, self.voice = self.levels() if self.talking else (0.0, 0.0)
        width, height = self._size()
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens() if _contains(s.frame(), mouse)), NSScreen.mainScreen())
        area = screen.visibleFrame()
        self.panel.setFrame_display_(
            NSMakeRect(area.origin.x + (area.size.width - width) / 2, area.origin.y + 8, width, height), False
        )
        self.view.setFrame_(NSMakeRect(0, 0, width, height))
        self.view.setNeedsDisplay_(True)
        if not self.visible:
            self.panel.orderFrontRegardless()
            self.visible = True

    def _size(self) -> tuple[int, int]:
        rows = len(self.jobs_shown) * JOB_ROW
        if self.talking:
            return FULL_SIZE[0], FULL_SIZE[1] + (rows + 4 if rows else 0)
        return FULL_SIZE[0], 30 + rows


def _contains(frame, point) -> bool:
    return (
        frame.origin.x <= point.x <= frame.origin.x + frame.size.width
        and frame.origin.y <= point.y <= frame.origin.y + frame.size.height
    )
