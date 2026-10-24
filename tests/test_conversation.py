from elevenlabs.conversational_ai.conversation import Conversation

from jarvis.__main__ import RealtimeConversation


def test_reply_audio_after_an_interruption_is_kept(monkeypatch):
    def sdk_handle(self, message, ws):
        if message["type"] == "interruption":
            self._last_interrupt_id = int(message["interruption_event"]["event_id"])

    monkeypatch.setattr(Conversation, "_handle_message", sdk_handle)
    conversation = RealtimeConversation.__new__(RealtimeConversation)
    conversation._last_interrupt_id = 0
    conversation._handle_message({"type": "interruption", "interruption_event": {"event_id": "34"}}, None)
    # The SDK drops audio with event_id <= this; the reply arrives as event 34 and must play.
    assert conversation._last_interrupt_id == 33
