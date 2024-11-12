from .base import VoiceAssistant
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
import transformers
import torch
import io
import base64
from openai import OpenAI
import soundfile as sf


class GPT4oAssistant(VoiceAssistant):
    def __init__(self):
        self.client = OpenAI()

    def generate_audio(
        self,
        audio,
    ):
        # Write the audio data to an in-memory buffer in WAV format
        buffer = io.BytesIO()
        sf.write(buffer, audio['array'], audio['sampling_rate'], format='WAV')
        buffer.seek(0)  # Reset buffer position to the beginning

        # Read buffer as bytes and encode in base64
        wav_data = buffer.read()
        encoded_string = base64.b64encode(wav_data).decode('utf-8')

        completion = self.client.chat.completions.create(
            model="gpt-4o-audio-preview",
            modalities=["text"],
            max_tokens=1024,
            messages=[
                {"role": "system", "content": "You are a helpful assistant who tries to help answer the user's question."},
                {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": encoded_string, "format": 'wav'}}]},
            ]
        )

        return completion.choices[0].message.content