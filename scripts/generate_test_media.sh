#!/usr/bin/env bash
# Generates original, locally owned test media (no third-party content) with ffmpeg + espeak-ng.
# Usage: scripts/generate_test_media.sh [out_dir] [--long]   (--long adds a 3-minute 720p gameplay-style file)
set -euo pipefail
OUT="${1:-outputs/test-media}"; LONG="${2:-}"
mkdir -p "$OUT"
command -v ffmpeg >/dev/null || { echo "ffmpeg is required" >&2; exit 1; }
command -v espeak-ng >/dev/null || { echo "espeak-ng is required" >&2; exit 1; }
say() { espeak-ng -s 150 -w "$2" "$1"; }
ff() { ffmpeg -hide_banner -loglevel error -y "$@"; }

# 1. 1080p H.264 talk: several distinct spoken segments separated by pauses (highlight ranking input).
say "Welcome to the so-test broadcast. This original synthetic speech created for automated testing." "$OUT/t1.wav"
say "Wait, did you see that? That was incredible. The score just changed in the final seconds." "$OUT/t2.wav"
say "Now let's talk about why this matters for creators. Here is the key lesson. Always keep your rights records current." "$OUT/t3.wav"
say "Thanks for watching and remember to subscribe." "$OUT/t4.wav"
ff -i "$OUT/t1.wav" -i "$OUT/t2.wav" -i "$OUT/t3.wav" -i "$OUT/t4.wav" -filter_complex \
  "[0]apad=pad_dur=0.8[a0];[1]apad=pad_dur=0.8[a1];[2]apad=pad_dur=0.8[a2];[3]apad=pad_dur=0.5[a3];[a0][a1][a2][a3]concat=n=4:v=0:a=1[a]" \
  -map "[a]" -ar 48000 "$OUT/talk.wav"
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/talk.wav")
ff -f lavfi -i "smptehdbars=size=1920x1080:rate=30" -f lavfi -i "testsrc2=size=1920x1080:rate=30" -i "$OUT/talk.wav" \
  -filter_complex "[0][1]blend=all_expr='if(gt(T,8),B,A)'[v]" -map "[v]" -map 2:a -t "$DUR" -c:v libx264 -pix_fmt yuv420p -c:a aac "$OUT/talk_1080p_h264.mp4"

# 2. 720p QuickTime "cooking" style with speech; 3. 480p WebM (VP9/Opus).
say "First we chop the onions. Careful now, this pan is very hot! Add the garlic and stir for thirty seconds." "$OUT/c.wav"
ff -f lavfi -i "testsrc=size=1280x720:rate=25" -i "$OUT/c.wav" -af apad -t 22 -c:v libx264 -pix_fmt yuv420p -c:a aac "$OUT/cooking_720p.mov"
ff -i "$OUT/talk_1080p_h264.mp4" -vf scale=854:480 -c:v libvpx-vp9 -b:v 600k -deadline realtime -cpu-used 8 -c:a libopus "$OUT/talk_480p.webm"

# 4. Vertical 9:16 with music-like tone and no speech; 5. silent video with no audio stream (edge cases).
ff -f lavfi -i "mandelbrot=size=720x1280:rate=30" -f lavfi -i "sine=frequency=440:sample_rate=48000" -t 15 -c:v libx264 -pix_fmt yuv420p -c:a aac "$OUT/vertical_nospeech_15s.mp4"
ff -f lavfi -i "color=c=navy:size=640x360:rate=30" -t 8 -an -c:v libx264 -pix_fmt yuv420p "$OUT/silent_noaudio_8s.mp4"

if [ "$LONG" = "--long" ]; then
  say "Round one begins. Huge play! What a comeback! That was the best move of the match. Let's go again." "$OUT/g.wav"
  ff -f lavfi -i "testsrc2=size=1280x720:rate=60" -stream_loop -1 -i "$OUT/g.wav" -t 180 -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac "$OUT/gameplay_3min_720p.mp4"
fi
rm -f "$OUT"/*.wav
(cd "$OUT" && sha256sum ./*.mp4 ./*.mov ./*.webm > SHA256SUMS)
echo "generated in $OUT"; cat "$OUT/SHA256SUMS"
