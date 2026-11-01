# Changelog

**English** · [Русский](CHANGELOG.ru.md)

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Fixed

- **Recording retention also removed clips you cut** and videos with the
  chat burned in, together with the recording they came from. They are now
  left alone: they were kept on purpose.
- The tray icon tells you when the stream beginning is joined to the
  recording, or why not: the join takes minutes and the window is usually
  hidden by then.
- **Downloading a section of a video** (and the Twitch and Kick stream
  beginning) showed only "downloaded N MB": ffmpeg reported no progress.
  Now it shows the percent of the section, speed and time left.
- **The "Channel" filter in the library** was empty — it listed no
  channels.
- **Enter in the search box on plain text** ("lofi girl") opened the page
  of a nonexistent Twitch channel, with auto-record and settings. A name
  impossible on the platform is no longer taken for a channel, and Enter
  shows the search results.
- A YouTube stream beginning for a video without an index failed; it now
  downloads the old way, through ffmpeg.

## 0.4.0 — 2026-11-01

### Added

- **Stream beginning join status** in Downloads: "joining to the recording:
  N%", then "joined to the recording" or why not — with a notification.
  A joined download opens the joined recording (also one joined by the
  previous version).
- **Speed and time left** for the VK and YouTube stream beginning download.

### Fixed

- The chat of a YouTube stream beginning without chat replay showed the
  error "The downloaded file is empty"; now it says the video's chat has no
  messages.

## 0.3.2 — 2026-11-01

### Fixed

- **The YouTube stream beginning still failed** ("ffmpeg exited with code
  4294957242"): the ffmpeg reconnect from 0.3.1 did not help, YouTube kept
  dropping the connection. The program now downloads the YouTube beginning
  itself: exactly the needed bytes by the file's index, in 10 MB parts, a
  drop is retried from the same position; ffmpeg only puts video and audio
  together, without the network. Verified: the published recording's
  beginning joins with the stream recording (seam found, length as on
  YouTube).
- **Closing the program in the middle of joining the stream beginning**
  left its temporary files ("….joining.ts", gigabytes) next to the
  recording, and they showed up in the library. They are now hidden from
  the library and removed at start, and the join starts over.

## 0.3.1 — 2026-11-01

### Fixed

- **The VK Video Live stream beginning did not download** ("ffmpeg exited
  with code 4294967291"): ffmpeg did not trust the certificate of VK's
  recording servers, whose root is missing from the Windows store until a
  browser fetches it. The program now downloads the VK beginning itself,
  with the same certificate checks as yt-dlp and streamlink.
- **The YouTube stream beginning failed after half a minute** ("ffmpeg
  exited with code 4294957242"): YouTube dropped the connection and the
  download gave up. ffmpeg now reconnects at the same position.

## 0.3.0 — 2026-11-01

### Added

- **Recording works the same for every channel:** "Start recording",
  "Autorecord" and "Recording settings" are on any channel's page, not only
  on channels added to the list.
- **A link to a live stream** (YouTube `watch?v=…`) opens the channel page
  with the stream and "Start recording" instead of offering a download.
- **A link to a stream on regular VK Video** (`vkvideo.ru/live-…`,
  `vk.com/video-…`) finds the same stream on VK Video Live — VK shows
  VK Video Live streams on its own site too — and opens that channel with
  "Start recording", chat and the stream beginning. Before, the link opened
  a nonexistent channel "live-…". A stream that has ended can be downloaded
  as a video.

### Changed

- **"Follow" is now "Add to favorites":** the button only adds the channel
  to the left panel ("Favorites"). Above it, "Recording now" lists every
  channel being recorded, in favorites or not.
  Removing a channel from favorites needs no confirmation any more: a
  recording in progress goes on, autorecord and own settings stay. All
  channels from the previous version are in favorites.

### Fixed

- **Watching a recording that is still being written** always played from
  its beginning: opening it and every click on the timeline started at
  0:00 while the timeline showed the chosen spot (broken since 0.1.2).
  Now it starts exactly where you click, in a fraction of a second, on all
  platforms.
- **No stream frame on VK Video Live:** when the streamer has not uploaded
  a stream cover, the channel cover is shown instead of an empty frame —
  VK Video Live shows that cover as the stream picture and gives no live
  frame.
- **The stream beginning on YouTube and VK could be lost:** if the platform
  did not answer in time, waiting for its recording stopped for good, and
  closing the program before the platform published the recording ended it
  too. Now a failure means trying again later, and what is awaited is kept
  in `beginning.json` and awaited again after a restart (up to 6 hours
  after the stream ended).
- **When the program was ended in Task Manager,** streamlink kept recording
  the stream on its own (without chat), and after a restart the program
  started a second recording of the same stream; ffmpeg and yt-dlp kept
  running too. Now everything the program started closes with it, and what
  was recorded is picked up a couple of minutes after the next start
  instead of ten.
- **Shutting down or restarting Windows** had to wait for the program: the
  window hid to the tray, and Windows killed it in the middle of a
  recording. Now the program closes recordings and downloads right away,
  and conversion finishes on the next start.
- A channel's own recording settings could disappear from the window until
  a restart after any event of its recording, and the window would show
  the general values.

## 0.2.1 — 2026-11-01

### Fixed

- **Frame previews on the player's timeline** were up to half the step
  ahead of the spot under the mouse: on a two-hour stream, 6–8 seconds
  later than where a click would land. Each preview now shows exactly its
  moment.
- **The stream beginning on YouTube** was downloaded in VP9 or AV1, while
  the recording is H.264, so the two parts could not be joined. The
  beginning is now taken in the recording's codecs (H.264 and AAC) when the
  platform has them — Twitch, Kick and VK Video Live already did.

## 0.2.0 — 2026-11-01

### Added

- **Convert to mp4 asks how to split:** the button on a `.ts` card opens a
  window to choose not to split, by size, by duration or into equal
  parts, with the channel's setting chosen to begin with.
- **The stream beginning is joined with the recording** into one file
  when both are ready (setting "Join the stream beginning with the
  recording", on by default, also per channel). The seam is found by
  comparing frames, so there is no repeat or gap; nothing is re-encoded,
  the chat is joined too, and the parts are deleted. If the frame size or
  codec differ, or the seam is not found, the parts stay side by side
  and the log says why. Checked on live Twitch and Kick streams: every
  frame before and after the seam matches the sources.

### Fixed

- After an update the desktop and Start menu shortcuts kept the old icon from
  the Windows icon cache; the installer now tells Explorer to refresh icons.
- A recording left in `.ts` with its chat ready, then split by Convert to mp4,
  kept one chat for the whole recording; now every piece gets its own.

## 0.1.4 — 2026-10-31

### Changed

- **Channel recording settings** show the actual values with the general
  settings chosen to begin with, instead of "as in settings: …" lines.
  A value equal to the general setting is not stored for the channel, so
  changing the general settings still changes it. The same in the Add
  channel window.

### Fixed

- **A short stream drop no longer ends the recording.** When the
  streamer reconnects, the platform briefly reports the stream as
  offline; a recording started with the button used to stop for good.
  Now the app waits up to 5 minutes, checking every 15 seconds, and
  continues with the next part of the same stream.

## 0.1.3 — 2026-10-31

### Changed

- **Live status line in the console:** `record` shows each recording's
  time, size and chat count with a blinking REC dot (or a spinner and
  how many channels are live), plus what is being processed; `download`
  shows a progress bar with speed and time left. It is redrawn in place,
  log lines go above it, and it is off when the output goes to a file.
- **Console commands in English:** `record`, `download`, `info`,
  `platforms` and `--help` speak the window's language — from the
  settings, or the Windows language until it is chosen. argparse's own
  headings and errors ("usage", "options") are translated too.
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
