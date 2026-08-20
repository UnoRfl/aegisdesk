"""DOM KeyboardEvent.code -> PS/2 set-1 scancode (+ extended flag).

Scancodes rather than virtual keys: they survive keyboard-layout differences
between the operator's machine and the remote one, and they are what games,
UAC prompts and nested RDP sessions actually respond to.
"""

# code -> (scancode, extended)
SCANCODES = {
    "Escape": (0x01, 0), "Digit1": (0x02, 0), "Digit2": (0x03, 0), "Digit3": (0x04, 0),
    "Digit4": (0x05, 0), "Digit5": (0x06, 0), "Digit6": (0x07, 0), "Digit7": (0x08, 0),
    "Digit8": (0x09, 0), "Digit9": (0x0A, 0), "Digit0": (0x0B, 0),
    "Minus": (0x0C, 0), "Equal": (0x0D, 0), "Backspace": (0x0E, 0), "Tab": (0x0F, 0),
    "KeyQ": (0x10, 0), "KeyW": (0x11, 0), "KeyE": (0x12, 0), "KeyR": (0x13, 0),
    "KeyT": (0x14, 0), "KeyY": (0x15, 0), "KeyU": (0x16, 0), "KeyI": (0x17, 0),
    "KeyO": (0x18, 0), "KeyP": (0x19, 0),
    "BracketLeft": (0x1A, 0), "BracketRight": (0x1B, 0), "Enter": (0x1C, 0),
    "ControlLeft": (0x1D, 0),
    "KeyA": (0x1E, 0), "KeyS": (0x1F, 0), "KeyD": (0x20, 0), "KeyF": (0x21, 0),
    "KeyG": (0x22, 0), "KeyH": (0x23, 0), "KeyJ": (0x24, 0), "KeyK": (0x25, 0),
    "KeyL": (0x26, 0), "Semicolon": (0x27, 0), "Quote": (0x28, 0), "Backquote": (0x29, 0),
    "ShiftLeft": (0x2A, 0), "Backslash": (0x2B, 0),
    "KeyZ": (0x2C, 0), "KeyX": (0x2D, 0), "KeyC": (0x2E, 0), "KeyV": (0x2F, 0),
    "KeyB": (0x30, 0), "KeyN": (0x31, 0), "KeyM": (0x32, 0),
    "Comma": (0x33, 0), "Period": (0x34, 0), "Slash": (0x35, 0), "ShiftRight": (0x36, 0),
    "NumpadMultiply": (0x37, 0), "AltLeft": (0x38, 0), "Space": (0x39, 0), "CapsLock": (0x3A, 0),
    "F1": (0x3B, 0), "F2": (0x3C, 0), "F3": (0x3D, 0), "F4": (0x3E, 0), "F5": (0x3F, 0),
    "F6": (0x40, 0), "F7": (0x41, 0), "F8": (0x42, 0), "F9": (0x43, 0), "F10": (0x44, 0),
    "NumLock": (0x45, 0), "ScrollLock": (0x46, 0),
    "Numpad7": (0x47, 0), "Numpad8": (0x48, 0), "Numpad9": (0x49, 0), "NumpadSubtract": (0x4A, 0),
    "Numpad4": (0x4B, 0), "Numpad5": (0x4C, 0), "Numpad6": (0x4D, 0), "NumpadAdd": (0x4E, 0),
    "Numpad1": (0x4F, 0), "Numpad2": (0x50, 0), "Numpad3": (0x51, 0),
    "Numpad0": (0x52, 0), "NumpadDecimal": (0x53, 0),
    "IntlBackslash": (0x56, 0), "F11": (0x57, 0), "F12": (0x58, 0),
    "F13": (0x64, 0), "F14": (0x65, 0), "F15": (0x66, 0),
    "IntlRo": (0x73, 0), "IntlYen": (0x7D, 0), "Convert": (0x79, 0), "NonConvert": (0x7B, 0),
    "KanaMode": (0x70, 0), "Lang1": (0x72, 0), "Lang2": (0x71, 0),
    # extended (0xE0-prefixed) keys
    "NumpadEnter": (0x1C, 1), "ControlRight": (0x1D, 1), "NumpadDivide": (0x35, 1),
    "AltRight": (0x38, 1), "PrintScreen": (0x37, 1), "Home": (0x47, 1), "ArrowUp": (0x48, 1),
    "PageUp": (0x49, 1), "ArrowLeft": (0x4B, 1), "ArrowRight": (0x4D, 1), "End": (0x4F, 1),
    "ArrowDown": (0x50, 1), "PageDown": (0x51, 1), "Insert": (0x52, 1), "Delete": (0x53, 1),
    "MetaLeft": (0x5B, 1), "MetaRight": (0x5C, 1), "ContextMenu": (0x5D, 1),
    "Pause": (0x45, 1), "BrowserBack": (0x6A, 1), "BrowserForward": (0x69, 1),
    "AudioVolumeMute": (0x20, 1), "AudioVolumeDown": (0x2E, 1), "AudioVolumeUp": (0x30, 1),
    "MediaPlayPause": (0x22, 1), "MediaTrackNext": (0x19, 1), "MediaTrackPrevious": (0x10, 1),
}

MODIFIER_CODES = {
    "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight",
    "AltLeft", "AltRight", "MetaLeft", "MetaRight", "CapsLock",
}

# code -> pynput key name or literal character, for the non-Windows backend
PYNPUT_SPECIAL = {
    "Escape": "esc", "Backspace": "backspace", "Tab": "tab", "Enter": "enter",
    "NumpadEnter": "enter", "Space": "space", "CapsLock": "caps_lock",
    "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r",
    "AltLeft": "alt_l", "AltRight": "alt_gr",
    "MetaLeft": "cmd_l", "MetaRight": "cmd_r", "ContextMenu": "menu",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Home": "home", "End": "end", "PageUp": "page_up", "PageDown": "page_down",
    "Insert": "insert", "Delete": "delete", "PrintScreen": "print_screen",
    "ScrollLock": "scroll_lock", "NumLock": "num_lock", "Pause": "pause",
    **{f"F{i}": f"f{i}" for i in range(1, 21)},
}

PYNPUT_CHARS = {
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]",
    "Semicolon": ";", "Quote": "'", "Backquote": "`", "Backslash": "\\",
    "Comma": ",", "Period": ".", "Slash": "/", "IntlBackslash": "\\",
    "NumpadMultiply": "*", "NumpadAdd": "+", "NumpadSubtract": "-", "NumpadDivide": "/",
    "NumpadDecimal": ".",
    **{f"Digit{d}": str(d) for d in range(10)},
    **{f"Numpad{d}": str(d) for d in range(10)},
    **{f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
}
