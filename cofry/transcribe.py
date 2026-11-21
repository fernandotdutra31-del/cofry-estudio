import argparse
from pathlib import Path
import json
import wave

import numpy as np
from faster_whisper import WhisperModel


def transcribe(source, model, threads, language, target):
    whisper = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
    words = []
    with wave.open(source, "rb") as audio:
        rate = audio.getframerate()
        for frame in range(0, audio.getnframes(), rate * 180):
            audio.setpos(frame)
            raw = audio.readframes(rate * 180)
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            segments, info = whisper.transcribe(samples, language=language,
                                               word_timestamps=True, vad_filter=True)
            if language is None:
                language = info.language
            offset = frame / rate
            for segment in segments:
                for word in segment.words or ():
                    token = word.word.strip()
                    if token and word.end > word.start:
                        words.append({"inicio": word.start + offset, "fim": word.end + offset, "texto": token})
            percent = round(100 * min(audio.getnframes(), frame + rate * 180) / audio.getnframes())
            Path("transcribe.tmp").write_text(str(percent))
            Path("transcribe.tmp").replace("transcribe.progress")
    with open(target, "w", encoding="utf-8") as out:
        json.dump(words, out, ensure_ascii=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("threads", type=int)
    parser.add_argument("language")
    args = parser.parse_args()
    transcribe("speech.wav", args.model, args.threads,
               None if args.language == "auto" else args.language.split("-")[0], "words.json")
