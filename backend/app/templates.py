"""Edit templates: framing plus caption style. Selected by SOE_EDIT_TEMPLATE; brand text by SOE_BRAND_TEXT."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    name: str
    framing: str  # "fit" letterboxes the whole frame; "action_crop" crops 9:16 around the motion centroid
    font: str
    font_size: int
    primary: str  # ASS &HAABBGGRR
    bold: bool
    outline: int
    margin_v: int


TEMPLATES = {
    "clean": Template("clean", "fit", "DejaVu Sans", 64, "&H00FFFFFF", False, 3, 260),
    "bold_captions": Template("bold_captions", "fit", "DejaVu Sans", 84, "&H0000F0FF", True, 5, 420),
    "gameplay_focus": Template("gameplay_focus", "action_crop", "DejaVu Sans", 72, "&H00FFFFFF", True, 4, 300),
}


def get(name: str) -> Template:
    return TEMPLATES.get(name, TEMPLATES["clean"])


def video_filter(template: Template, crop_center: float | None, subtitle_path=None) -> str:
    if template.framing == "action_crop" and crop_center is not None:
        center = min(1.0, max(0.0, crop_center))
        # Crop a 9:16 column from landscape sources; for already-vertical sources the crop is the full frame.
        vf = (f"crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{center:.4f}':0,"
              "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")
    else:
        vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if subtitle_path:
        path = str(subtitle_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf += f",subtitles='{path}'" if str(subtitle_path).endswith(".srt") else f",ass='{path}'"
    return vf
