# Changelog

**English** · [Русский](CHANGELOG.ru.md)

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- **Console commands in English:** `record`, `download`, `info`,
  `platforms` and `--help` speak the window's language — from the
  settings, or the Windows language until it is chosen.
- **New logo:** a safe door on hinges with a combination lock and a play
  button, and a red recording dot in the corner — in the window, the
  exe, the installer and the README. The tray icon shows the dot only
  while something is recording.
- **"Start recording" answers right away:** while streamlink connects to
  the stream (a few seconds), the button shows a spinner and "Recording
  is starting…", the live card shows "Starting…". A second click cancels.
  The status line under the channel name says "recording is starting…"
  too.
- **Channel search on VK Video Live** finds channels by part of the name
  or nick, like the site's own search, with who is live, viewers and the
  category. It used to check only the exact channel name.
- **Downloads go to the channel's folder by its ID** (login, @handle,
  name in the address) — the same folder as its recorded streams. A
  YouTube video of Lofi Girl used to land in "Lofi Girl" while the
  recordings were in "LofiGirl".

### Fixed

- The window took port 42001, the one every pywebview app uses by
  default. If another such program was already running there, the
  window could silently show its pages. The port is now a free one
  chosen by Windows.

## 0.1.2 — 2026-10-31

### Changed

- **Library:** a click on a card opens the recording in the app's player
  with chat; the play icon on the card opens it in the Windows player.
- **Drop-down lists** are drawn by the app instead of Windows: even
  arrows, a list in the window's style with a check mark on the chosen
  item, smooth opening, and opening upwards when there is no room below.
  The keyboard works as before.

### Fixed

- **Watching a YouTube recording in the window** — live or left in `.ts` —
  stopped with a decode error: at low quality YouTube sends HE-AAC sound,
  and the stream told the player wrong sound settings.
- **Watching a recording that is still being written** starts within a
  couple of seconds on every platform. On YouTube the first frame used to
  take 10–15 s, and the player gave up and asked again in a loop.

## 0.1.1 — 2026-10-31

### Added

- **Installer** `StreamVault-<version>-setup.exe`: pick the folder, Start
  menu and desktop shortcuts, uninstall from Windows settings. It installs
  for the current user without admin rights, or for all users. An installed
  copy updates itself by downloading and running the new installer.

### Fixed

- The window did not open when the archive was unpacked with Windows
  Explorer: Windows marks every unpacked file as downloaded from the
  internet, and .NET refused to load the window's library. The app now
  removes this mark from its own files on start.

## 0.1.0 — 2026-10-31

The first public version.

### Added

- **Live recording** on Twitch, Kick, YouTube and VK Video Live: a
  "Start recording" button or auto-record, per-channel recording settings,
  continuing as a new part after a dropped connection, MP4 conversion
  without re-encoding and splitting into pieces.
- **Recording from the start of a stream:** the part before the recording
  began is downloaded from the platform's archive.
- **Live chat** of all four platforms next to the recording; VOD and clip
  chat as HTML, JSON or a chat video.
- **Downloads** of VODs, highlights and clips with quality choice, sections,
  "Download all" and resuming after a restart.
- **Channel search** on all four platforms at once, avatars and live
  previews.
- **Library and player** with synced chat, resume position, shortcuts,
  watching a recording while it is still being written, clips and chat
  overlay.
- **Disk space guard**, recording retention and gap checks.
- Tray, notifications, start with Windows, English and Russian interface,
  updates from GitHub releases and yt-dlp updates from the window.
