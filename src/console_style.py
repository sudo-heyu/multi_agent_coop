import os


RESET = "\033[0m"
BOLD = "\033[1m"
FG = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "white": "\033[37m",
    "gray": "\033[90m",
}

AP_NAME_COLORS = {
    "AP1": FG["cyan"],
    "AP2": FG["green"],
    "AP3": FG["yellow"],
}

AP_CONTENT_COLORS = {
    "AP1": "\033[96m",
    "AP2": "\033[92m",
    "AP3": "\033[93m",
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


def format_ap_content(speaker: str, content: str) -> str:
    ap = ap_key(speaker)
    return color(content, AP_CONTENT_COLORS.get(ap, FG["white"]))


def ap_content_color(speaker: str) -> str:
    if not _enabled():
        return ""
    ap = ap_key(speaker)
    return AP_CONTENT_COLORS.get(ap, FG["white"])


def reset() -> str:
    return RESET if _enabled() else ""


def divider(width: int = 72) -> str:
    return color("-" * width, FG["gray"])


def section(title: str) -> str:
    return color(f"[{title}]", BOLD, FG["blue"])


def status_label(label: str) -> str:
    return color(label, BOLD, FG["magenta"])
