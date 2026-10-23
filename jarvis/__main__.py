import argparse
import threading
import time

import openwakeword
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from openwakeword.model import Model

from .agent import api_key, ensure_agent
from .audio import SoundDeviceAudio, wait_for_wake_word

IDLE_SECONDS = 12  # silence after Jarvis stops talking before the session closes (sessions bill per minute)


def converse(client: ElevenLabs, agent_id: str):
    audio = SoundDeviceAudio()
    ended = threading.Event()
    last_user = time.monotonic()

    def on_user(text):
        nonlocal last_user
        last_user = time.monotonic()
        print(f"you: {text}")

    conversation = Conversation(
        client,
        agent_id,
        requires_auth=True,
        audio_interface=audio,
        callback_user_transcript=on_user,
        callback_agent_response=lambda text: print(f"jarvis: {text}"),
        callback_end_session=ended.set,
    )
    conversation.start_session()
    while not ended.wait(1):
        if time.monotonic() - max(last_user, audio.last_played) > IDLE_SECONDS:
            conversation.end_session()
    conversation.wait_for_session_end()


def main():
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--push", action="store_true", help="press Enter to talk instead of saying 'Hey Jarvis'")
    args = parser.parse_args()

    client = ElevenLabs(api_key=api_key())
    agent_id = ensure_agent(client)
    model = None if args.push else Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
    try:
        while True:
            if args.push:
                input("Press Enter to talk... ")
            else:
                print("Listening for 'Hey Jarvis'...")
                wait_for_wake_word(model)
            converse(client, agent_id)
    except KeyboardInterrupt:
        pass


main()
