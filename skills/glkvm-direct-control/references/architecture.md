# Direct-control architecture

## Control path

The helper talks only to the GL.iNet Comet:

```text
AI/controller Mac -- HTTPS --> Comet -- HDMI capture <-- target Mac
                                  |
                                  +-- USB keyboard and mouse HID --> target Mac
```

This keeps observation and input independent of the target operating system. The target can be locked, at the macOS login window, or unable to run a screen-sharing service.

## API map

- `POST /api/auth/login`: exchange the local Comet admin password for an auth token.
- `GET /api/hid`: keyboard and mouse presence, online state, and active mouse mode.
- `GET /api/streamer/snapshot?allow_offline=true`: request a current JPEG; it also starts the capture pipeline on demand.
- `POST /api/hid/set_params?mouse_output=usb`: select absolute USB mouse mode.
- `POST /api/hid/events/send_mouse_move`: send absolute coordinates in the signed 16-bit KVM range.
- `POST /api/hid/events/send_mouse_button`: click a USB mouse button.
- `POST /api/hid/events/send_mouse_wheel`: send USB wheel movement.
- `POST /api/hid/events/send_key`: press and release one web key code.
- `POST /api/hid/events/send_shortcut`: press and release a key chord.
- `POST /api/hid/print`: type mapped text without using a clipboard.
- `POST /api/upgrade/reboot`: reboot the Comet itself.
- `POST /api/streamer/reset`: restart only the Comet capture pipeline.

The helper accepts pixels from the returned screenshot and maps each axis to `-32768..32767`:

```text
absolute = round(pixel / (dimension - 1) * 65535 - 32768)
```

## Health interpretation

Healthy input requires all of these to be true in `/api/hid`:

- `connected`
- `enabled`
- `online`
- `keyboard.online`
- `mouse.online`

The streamer object can be null when nobody is watching. That is not a fault. Request a snapshot to test video. If HDMI is asleep, wake through HID and retry before considering recovery.

Do not use `/api/system/otg_functions` as a recovery mechanism. On the current firmware, disabling and re-enabling HID can fail to find a USB device controller. A Comet reboot is the proven recovery when HID is genuinely unhealthy.

## Credential model

The helper reads two separate macOS Keychain generic-password items:

- `glkvm-comet`, account `admin`: Comet web login.
- `glkvm-target-login`, account `husky`: optional password for the controlled Mac.

The target-login item is only read by `login-target`; ordinary screenshot and input commands never access it. Environment variable `GLKVM_PASSWORD` can override the Comet Keychain item for ephemeral automation, but Keychain is preferred.

## Known boundaries

- The controller needs network reachability to the Comet.
- A controller running on the target Mac cannot act before that Mac's user-session automation is running.
- Visual reasoning belongs to the calling AI. The helper supplies screenshots and deterministic input primitives; it does not guess dialog locations.
- The `open-url` helper assumes a browser is foregrounded and the desktop is unlocked.
