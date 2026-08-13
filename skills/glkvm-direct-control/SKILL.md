---
name: glkvm-direct-control
description: Inspect and operate the GL.iNet Comet KVM directly over the network using its video capture and USB keyboard/mouse HID APIs. Use for over-the-top control of the attached Mac at lock/login screens, closing blocking dialogs, recovering failed KVM input, waking the display, taking KVM screenshots, or clicking, typing, scrolling, and opening sites when ordinary remote-control software is unavailable. Do not use an Android phone for this workflow.
---

# GLKVM Direct Control

Operate the controlled computer through the Comet itself. The helper uses HTTPS snapshots for sight and emulated USB HID for input, so it does not depend on the controlled Mac's desktop, accessibility permissions, browser extension, SSH service, or phone.

## Use the helper

Set this once in the shell running the skill:

```bash
GLKVM_SCRIPT="$HOME/.openclaw/skills/glkvm-direct-control/scripts/glkvm_control.py"
```

The default Comet address is `192.168.1.67`. Override it by placing `--host ADDRESS` before the command. The helper prints JSON and exits nonzero on failure.

Before the first use on a controller Mac, run this interactively so the Comet password is stored in macOS Keychain rather than the skill or shell history:

```bash
python3 "$GLKVM_SCRIPT" configure
```

Never put either the Comet password or the target Mac password in a prompt, command argument, log, screenshot filename, or skill file.

## Standard control loop

For every visual operation:

1. Check HID health.
2. Take and inspect a fresh screenshot.
3. Apply the smallest input action.
4. Take and inspect another screenshot to prove the result.

```bash
python3 "$GLKVM_SCRIPT" status
python3 "$GLKVM_SCRIPT" screenshot --output /tmp/glkvm-before.jpg
python3 "$GLKVM_SCRIPT" click --x 960 --y 540 --width 1920 --height 1080
python3 "$GLKVM_SCRIPT" screenshot --output /tmp/glkvm-after.jpg
```

Use the `width` and `height` returned by `screenshot` as the coordinate space. Inspect the image with the host's image-reading capability. Do not reuse coordinates after a resolution or screen-layout change.

## Keyboard and mouse

Use web `KeyboardEvent.code` values such as `Enter`, `Escape`, `Tab`, `KeyA`, `ArrowDown`, `MetaLeft`, and `ShiftLeft`.

```bash
python3 "$GLKVM_SCRIPT" key Escape
python3 "$GLKVM_SCRIPT" shortcut MetaLeft,KeyL
python3 "$GLKVM_SCRIPT" type 'text to enter' --slow
python3 "$GLKVM_SCRIPT" click --x 640 --y 420 --width 1920 --height 1080
python3 "$GLKVM_SCRIPT" scroll --dy 6
```

Positive `--dy` scrolls down; negative scrolls up. `click` automatically selects the absolute USB mouse. Prefer one click or key at a time when dismissing unknown dialogs, then verify visually.

To operate a foreground browser using only USB HID:

```bash
python3 "$GLKVM_SCRIPT" open-url 'https://example.com'
python3 "$GLKVM_SCRIPT" screenshot --output /tmp/glkvm-site.jpg
```

Do not use SSH, AppleScript, browser automation, Android, VNC, RustDesk, or another desktop agent as a substitute when the task is specifically testing KVM control.

## Lock and login screens

This path works before desktop remote-control software is available. Take a screenshot first and confirm that the expected user and password field are visible.

The optional target-login credential is deliberately separate from the Comet credential. Configure it once through a private terminal prompt on the controller Mac:

```bash
python3 "$GLKVM_SCRIPT" configure-target-login
```

After visually focusing the correct password field, log in without printing the password:

```bash
python3 "$GLKVM_SCRIPT" login-target
```

If that credential is absent, stop at the login screen and ask the user to unlock locally or run `configure-target-login`. Never guess a password. Never report success until a new screenshot visibly shows the desktop.

## Wake and recover

`screenshot` automatically tries a harmless USB-HID wake when HDMI has no signal. A sleeping display with healthy HID does not justify rebooting the Comet.

```bash
python3 "$GLKVM_SCRIPT" wake
python3 "$GLKVM_SCRIPT" screenshot --output /tmp/glkvm-awake.jpg --wait 25
```

If `status` reports keyboard or mouse HID unhealthy, use the conservative recovery command:

```bash
python3 "$GLKVM_SCRIPT" recover --wait 180
```

`recover` does nothing when HID is already healthy. If unhealthy, it reboots the Comet, waits, and requires keyboard and mouse to return online. Never toggle HID OTG off and on as recovery; this firmware can lose its USB device controller until reboot. Do not reboot the attached Mac unless the user explicitly requests it.

If HID is healthy but screenshots still fail, recover the capture side separately:

```bash
python3 "$GLKVM_SCRIPT" recover-video --output /tmp/glkvm-recovered.jpg
```

This escalates from wake, to streamer reset, to a Comet-only reboot, and requires a real JPEG at the end. It never reboots the attached Mac.

## Reliability boundary

Install this skill on at least one other always-available controller. An OpenClaw process running on the controlled Mac cannot rescue that same Mac before its user session starts. A controller on the LAN can still reach the Comet at boot and login screens. A future Tailscale address can be supplied with `--host`, but do not change routing or expose the Comet remotely unless the user separately authorizes that setup.

For exact API mappings and failure behavior, read [references/architecture.md](references/architecture.md).
