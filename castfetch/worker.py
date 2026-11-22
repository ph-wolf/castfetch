"""MLX Whisper worker, executed using the configured transcription Python."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--cache-dir")
    parser.add_argument("--language")
    parser.add_argument(
        "--align-words",
        action="store_true",
        help="Retry with word alignment and without previous-window text conditioning.",
    )
    args = parser.parse_args()
    if args.cache_dir:
        os.environ["HF_HOME"] = args.cache_dir
    import mlx_whisper

    options = {"language": args.language, "verbose": False}
    if args.align_words:
        options.update(word_timestamps=True, condition_on_previous_text=False)
    result = mlx_whisper.transcribe(str(args.audio), path_or_hf_repo=args.model, **options)
    result["transcription_options"] = {
        "word_timestamps": args.align_words,
        "condition_on_previous_text": not args.align_words,
    }
    info = json.loads(
        subprocess.check_output(["ffprobe", "-v", "quiet", "-show_format", "-of", "json", str(args.audio)])
    )
    result["audio_duration"] = float(info["format"]["duration"])
    args.output.write_text(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
