from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SAFE_ACTIONS: dict[str, dict[str, Any]] = {
    "set_face": {"faces": {"idle", "happy", "sad", "dizzy", "sleepy", "alert"}},
    "set_expression": {
        "emotions": {"idle", "happy", "sad", "dizzy", "sleepy", "alert", "curious", "confused", "proud", "shy"}
    },
    "blink": {},
    "wave": {"level_min": 1, "level_max": 2},
    "small_step_forward": {"steps_min": 1, "steps_max": 3},
    "turn_left": {"steps_min": 1, "steps_max": 3},
    "turn_right": {"steps_min": 1, "steps_max": 3},
    "gentle_nudge": {"level_min": 1, "level_max": 1},
    "sit": {},
    "chirp": {"tones": {"soft", "happy", "alert", "low_battery"}},
    "sleep": {},
    "stop": {},
}

MOVEMENT_ACTIONS = {
    "wave",
    "small_step_forward",
    "turn_left",
    "turn_right",
    "gentle_nudge",
}

DANGEROUS_KEYWORDS = {"raw", "servo", "angle", "pwm", "flip", "push", "fight", "edge"}
DANGEROUS_FIELDS = DANGEROUS_KEYWORDS | {"servo_id", "current", "torque", "grip_force", "framebuffer", "pixels"}


@dataclass(frozen=True)
class ActionValidation:
    accepted: bool
    reason: str = ""


def validate_action(name: str, args: dict[str, Any] | None = None) -> ActionValidation:
    args = args or {}
    unsafe_field = find_dangerous_field(args)
    if unsafe_field:
        return ActionValidation(False, f"动作包含裸硬件字段: {unsafe_field}")
    if name not in SAFE_ACTIONS:
        return ActionValidation(False, f"未知或未授权动作: {name}")
    lowered = name.lower()
    if any(keyword in lowered for keyword in DANGEROUS_KEYWORDS):
        return ActionValidation(False, f"动作包含危险关键词: {name}")

    rule = SAFE_ACTIONS[name]
    if name == "set_face":
        face = args.get("face")
        if face not in rule["faces"]:
            return ActionValidation(False, f"不支持的表情: {face}")
    if name == "set_expression":
        emotion = args.get("emotion", "idle")
        if emotion not in rule["emotions"]:
            return ActionValidation(False, f"不支持的表情情绪: {emotion}")
    if "level_min" in rule:
        level = int(args.get("level", rule["level_min"]))
        if not rule["level_min"] <= level <= rule["level_max"]:
            return ActionValidation(False, f"动作强度越界: {level}")
    if "steps_min" in rule:
        steps = int(args.get("steps", rule["steps_min"]))
        if not rule["steps_min"] <= steps <= rule["steps_max"]:
            return ActionValidation(False, f"步数越界: {steps}")
    if "tones" in rule and args:
        tone = args.get("tone")
        if tone not in rule["tones"]:
            return ActionValidation(False, f"不支持的提示音: {tone}")
    return ActionValidation(True)


def is_movement_action(name: str) -> bool:
    return name in MOVEMENT_ACTIONS


def find_dangerous_field(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in DANGEROUS_FIELDS or any(keyword in lowered for keyword in DANGEROUS_FIELDS):
                return str(key)
            found = find_dangerous_field(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_dangerous_field(item)
            if found:
                return found
    return None
