import os
import re


RESET = "\033[0m"
BOLD  = "\033[1m"
FG = {
    "cyan":         "\033[36m",
    "green":        "\033[32m",
    "yellow":       "\033[33m",
    "blue":         "\033[34m",
    "magenta":      "\033[35m",
    "white":        "\033[37m",
    "gray":         "\033[90m",
    "red":          "\033[31m",
    "bright_red":   "\033[91m",
    "bright_green": "\033[92m",
}

AP_NAME_COLORS = {
    "AP1":         "\033[96m",   # bright cyan
    "AP2":         "\033[92m",   # bright green
    "AP3":         "\033[93m",   # bright yellow
    "COORDINATOR": "\033[95m",   # bright magenta
}

def _enabled() -> bool:
    return os.environ.get("AP_COOP_NO_COLOR") is None


def color(text: object, *codes: str) -> str:
    if not _enabled():
        return str(text)
    return "".join(codes) + str(text) + RESET


def ap_key(speaker: str) -> str | None:
    for ap in AP_NAME_COLORS:
        if speaker.startswith(ap):
            return ap
    return None


def format_ap_name(speaker: str) -> str:
    ap = ap_key(speaker)
    if not ap:
        return color(speaker, BOLD, FG["white"])
    return color(speaker, BOLD, AP_NAME_COLORS[ap])


def divider(width: int = 72) -> str:
    return color("-" * width, FG["gray"])


def section(title: str) -> str:
    return color(f"[{title}]", BOLD, FG["blue"])


def status_label(label: str) -> str:
    return color(label, BOLD, FG["magenta"])


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes (for test assertions on colored output)."""
    return re.sub(r'\033\[[0-9;]*m', '', text)


# ── Tool-output color helpers ─────────────────────────────────────────────

def tool_prefix() -> str:
    """Gray '[工具]' marker."""
    return color("[工具]", FG["gray"])


def tool_name(name: str) -> str:
    return color(name, BOLD, FG["gray"])


def tool_dur(ms: float) -> str:
    return color(f"({ms:.1f}ms)", FG["gray"])


def status_ok(text: str) -> str:
    return color(text, BOLD, FG["bright_green"])


def status_warn(text: str) -> str:
    return color(text, BOLD, FG["yellow"])


def status_fail(text: str) -> str:
    return color(text, BOLD, FG["bright_red"])


def ap_label(ap_id: str) -> str:
    """Color 'ap1:' etc. with the AP's designated color."""
    key = f"AP{ap_id[-1]}" if (ap_id.startswith("ap") and len(ap_id) == 3) else ap_id.upper()
    c = AP_NAME_COLORS.get(key, FG["white"])
    return color(ap_id + ":", c)


def dim(text: str) -> str:
    return color(text, FG["gray"])


def congestion_color(level: str) -> str:
    """Return the congestion level string colorized by severity."""
    palette = {
        "low":      (FG["green"],        False),
        "medium":   (FG["yellow"],       False),
        "high":     (FG["bright_red"],   False),
        "critical": (FG["bright_red"],   True),
    }
    c, bold = palette.get(level, (FG["white"], False))
    return color(level, BOLD, c) if bold else color(level, c)


def strip_md(text: str) -> str:
    """Remove common inline Markdown formatting for plain-text terminal display."""
    # **bold** / *italic* / ***bold-italic***
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text, flags=re.DOTALL)
    # `inline code`
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    # ## Heading (line-start)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # - bullet or * bullet (line-start)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    # 1. numbered list (line-start)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # > blockquote (line-start)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    return text
