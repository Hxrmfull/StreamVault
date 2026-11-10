# Changelog

**English** · [Русский](CHANGELOG.ru.md)

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## 0.16.0 — 2026-11-10

### Added

- Chat gaps are filled on Kick, YouTube and VK Video Live too, from the
  stream recording on the platform (only Twitch has the chat archive as
  well); dropped connections noted while recording are filled by
  themselves there as well. A Kick stream recording counts while the stream
  is still going.
- Deno in Settings → Tools: yt-dlp solves with it the JavaScript puzzles
  YouTube puts in front of videos, as a browser does. Without it YouTube
  gives fewer formats and is to stop downloading in time. A download button
  (about 40 MB, checksum checked on GitHub and in the scoop catalog), and
  updates like TwitchDownloader's. An installed Deno is found by itself, and
  without Deno yt-dlp may use an installed Node.js.

### Fixed

- Recording in the worst quality, the stream beginning was downloaded in
  the best one: gigabytes instead of megabytes, and it could not be joined
  (different frame size).
- With a long stream title and a long path to the recordings folder the
  stream beginning got a differently shortened name than the recording and
  never joined. Names are now cut alike, and a beginning that already has
  such a name is paired by channel and recording start time.
- "Fetch the stream beginning" was offered while the program itself was
  waiting for the stream recording to take the beginning (YouTube, VK
  Video Live): the beginning could download twice.
- Chat window, Viewers panel: YouTube names ("@name") showed no messages,
  and VK Video Live and YouTube names with spaces ("Анна Москва") typed into
  the field fell apart into several viewers.
- "Download chat" for a downloaded VK Video Live clip whose Downloads row is
  gone said "recording not found": by the id in the file name a clip looks
  like a stream recording. The id is now tried as a clip too.
- Downloading only a section from the middle of a VK Video Live recording
  failed ("ffmpeg exited with code …"): ffmpeg does not trust the CDN
  certificate. Such sections now download with the program's own requests,
  as the beginning already did.
- Filling chat gaps outside Twitch reported "emotes not embedded":
  TwitchDownloader does not understand other platforms' chats. Their emotes
  now come along with the filled messages.
- ffmpeg installed by another program was often not found: the search
  remembered "not found" until a restart, read PATH as it was when the
  program started (an installer adds to it later) and knew few folders.
  Now a missing ffmpeg is searched for again every minute, in PATH as
  Windows has it now, and in more places: folders like
  C:fmpeg-7.1-essentials_buildin, C:fmpeg without bin, Program Files
  (x86), the user's programs folder.
- Waiting for a stream recording to take its beginning asked the platform
  every 5 minutes for up to 6 hours; YouTube answers frequent requests with
  "confirm you're not a bot". Checks now get rarer, up to once in 30
  minutes.
- YouTube streams were placed by the time they were published (half an
  hour after the end) or created, not by when they started: finding the
  stream recording for the beginning, chat or gaps could miss it.

- "Fetch the stream beginning" was offered for a recording already joined
  with its beginning before 0.15.0 (the seam was not remembered then, and a
  later archive chat hid it): the beginning downloaded again and did not
  join, "seam not found", and stayed next to the recording. Now a
  recording longer than the stream ran after its start is recognised as
  joined, and a beginning that turns out to be inside the recording already
  (its first frames match) is removed instead of left next to it.
- Chat gaps and the archive chat of a recording joined with its beginning
  before 0.15.0 were taken from the wrong stretch: without the remembered
  seam the recording was thought to start when recording began, not with
  the stream. Nothing was added, and the gap was marked as checked ("the
  chat was silent"). The seam of such recordings is now found from the
  chat itself (message times are shifted by the beginning's length) and
  remembered, and their earlier "checked" marks are dropped so the gaps can
  be filled again.
- "Fetch the stream beginning" downloaded the beginning's chat even for a
  recording without chat, so after joining only the beginning had chat. The
  beginning now comes with chat only if the recording has one, as when
  recording from the start.
- The queue for the recording limit used a channel's priority from its last
  refusal: a channel added to favorites while waiting still went after a
  one-off recording that had waited longer. The priority is now taken fresh
  each time a slot frees up.

## 0.15.0 — 2026-11-09

### Added

- "Convert" works for any video, not only a recording left in .ts: mp4 ↔ mkv
  or sound only (m4a), without re-encoding, and split into parts at the same
  time if needed. The chat moves to the new file; the previous file goes to
  the whole folder (as its own card) or to the Windows Recycle Bin. Pieces
  and clips can be rewritten to another format too, though they still are
  not split.
- A limit on simultaneous recordings in Settings → Stream recording: in total
  and per platform, 0 for no limit (the default). A stream over the limit
  waits until a slot frees up, and its channel page says why. A freed slot
  goes to the queue, not to whoever asks first: a channel's own priority
  (high, normal, low in its recording settings) first, then channels with
  autorecord or from favorites before one-off recordings, then whoever has
  waited longest. Running recordings are never stopped for the queue.
- A channel's recording settings open as one wide window that fits on the
  screen: three sections (Recording, Stream beginning and keeping, After the
  stream), yes/no options as switches, dependent ones dimmed (joining without
  recording from the start, faststart for anything but mp4).
- Warnings on a running recording, in Downloads → Recordings and on the
  channel page: "the recording falls behind: the network or disk is too
  slow" (streamlink skips pieces of the stream) and "the platform seems to
  limit downloading" — when it answers "too many requests" (429) or denies
  access (403), or when only its recordings fall behind while those from
  other platforms keep up.
- "Convert…" and "Split into parts…" in a recording's menu are one item,
  "Edit…": a window with "Convert" and "Split into parts" tabs, what will
  happen to the recording below them and the settings in two columns.
- "Fetch the stream beginning" in a recording's menu: if the beginning was
  not downloaded while recording (the stream recording was hidden or not
  published yet), it can be fetched later. A click checks whether the
  stream recording is there now: if so, the beginning downloads and joins
  the recording by itself; if not, it says so.
- In the summary, "Most active in chat" and "Popular emotes" get "Show all":
  the whole ranked list opens in its own scrolling box with a search by
  name or emote, and the summary window does not stretch. It opens and
  closes smoothly.
- The left channel panel collapses and expands smoothly: the width slides,
  the labels fade, avatars and buttons stay in place.
- "Popular emotes" in the summary show the emote images, as in the chat
  window.
- A channel page has the platform's icon next to the name: a click opens the
  channel on the platform in the browser (instead of the platform label and
  the button on the right). The icon is grey in a frame and lights up in the
  platform's colour on hover.
- "Downloads" in the top menu shimmers in purple while something runs there:
  a recording, a join, a download or an edit, like the tab names.
- A recording's card shows the join strip before the join itself too:
  "joining after the recording ends" while recording and "preparing to
  join…" while it is processed.
- "Download chat" in the menu of a downloaded video without chat works even
  when its row is gone from Downloads (the list was cleared): the video link
  is built from the id in the file name. The chat lands next to the video,
  progress shows in the "Chats" tab.
- Exact connection drops noted while recording the chat are filled
  automatically once the recording's chat is ready (for a recording joined
  with the stream beginning, after the join). Twitch only for now. Guesses
  (silence, late start) still need the button: the chat may really have
  been silent there.

### Fixed

- An archive chat and filled chat gaps of a recording joined with the stream
  beginning were shifted by the beginning's length if its Downloads row had
  been removed: the seam came only from there. The join seam is now
  remembered separately.
- The result of filling chat gaps reads plainly: "messages added: N", and
  when there was nothing to add, "no missed messages found: the chat was
  silent then", instead of "messages added: 0; stretches where the chat
  really was silent: 1".
- While chat gaps were being filled, the card menu could offer filling again
  or say there were no gaps: the chat is already rewritten by then while
  emotes and HTML are still being made. It now says "chat gaps are being
  filled, progress in the Chats tab"; the same for an archive chat.
- The join strip's label ("joined, waiting for the player to close") ran
  into the card's edge: it is lighter now, fits, and a long one ends with an
  ellipsis.
- A Downloads row of an archive chat or chat gaps shows the recording's
  frame right away rather than at the end, and no longer has a stray
  "Download chat" button.
- The desktop icon jumped to a free spot after every update: the installer
  re-created the shortcut. An existing shortcut is now left alone on update.
- Dragging along the chat window's activity graph lagged behind the mouse,
  especially in big chats: each step rebuilt the whole feed, and the line
  jumped back to the not yet updated feed. Now the line stays under the
  mouse, a drag puts only the lines that fit on screen into the feed, and
  the rest loads once the button is released. In a 40,000-message chat
  that is about 8 ms per step instead of 130.
- During a fast drag along the graph the feed's scrollbar thumb jumped and
  stretched, since the feed is short meanwhile. It is now hidden while the
  button is held and comes back once the feed has loaded around the spot.
- The "Go to" field in the chat window: the caret sat between the zeros of
  an empty cell. It is now one field with a ready "00:00:00" mask: digits
  fill it from the left, typed ones bright and the rest dim, and the caret
  always right after the last digit. A "7" in the tens of minutes or seconds
  becomes "07", "1:" becomes "01:", Backspace erases the last digit. A time
  past the end of the chat cannot be typed: the digit is not taken, the
  field shakes and shows "The chat lasts …" below; a digit that does not
  fit as tens but fits as units goes into the units ("1" in a 4:46 chat
  becomes "01").

## 0.14.0 — 2026-11-09

### Added

- **Test versions:** "Get test versions" in Settings → Program → Updates.
  Test versions (0.14.0-beta.1 and so on) come out before stable ones and are
  offered only to those who turned this on; whichever is newer, test or
  stable, is offered. Everyone else keeps getting stable versions only.
  With a test version installed, "Back to stable …" next to "Check for
  updates" installs the latest stable version even if it is older, with the
  same checksum check; settings, library and downloads are kept.
- **Name suggestions in the chat window:** the "Add a name…" field suggests
  names that contain what you typed (those starting with it first), with
  their message counts; an empty field shows the most active viewers. The
  exact name is not needed. Arrows and Enter pick a suggestion.
- **Viewers panel in the chat window:** to the right of the feed, with the
  name field first. Clicking a name in the feed adds the viewer there, and
  their messages show next to the feed rather than instead of it; clicking
  such a message goes to it in the feed.
- **Dragging along the activity graph:** grab the white position line by its
  handle (or hold the mouse button anywhere on the graph) and move left or
  right, and the chat feed scrolls along.
- **Gaps in a recording's chat:** the live chat now notes when the
  connection to it dropped. The chat itself also shows other gaps: it
  starts noticeably later than the video, ends earlier or goes silent for a
  long time in the middle of a busy stretch. The threshold follows the pace
  of the same chat around the pause, so the usual pauses of quiet channels
  are not counted as gaps. The recording's "⋯" menu has "Fill chat gaps:
  N stretches": they come from the Twitch VOD, and what is not there from the
  chat archive, and are put into the chat only inside the gaps. If there are
  no messages there either, the stretch is remembered as checked ("the chat
  really was silent") and not offered again. The chat window hatches the
  gaps on the activity graph. Twitch only for now: Kick and YouTube will
  come later, VK Video Live has no source for them.
- **"Chats" tab in Downloads:** chats without video (chat only, from the
  third-party archive, filled gaps) are now apart from videos; "Clear list"
  removes only the rows of its own tab.
- **"Go to" without typing colons:** the field has three cells, hours,
  minutes and seconds, two digits each; after two digits the cursor moves
  on by itself. A pasted "1:23:45" is spread across the cells.

### Fixed

- A joined stream beginning open in a player (VLC, for example) kept the join
  waiting silently at its last step: the program saw the player but did not
  say so. Now it shows "joined, waiting for the file to be closed in …" for
  any file it waits for.
- The join strip on a library card blended into purple frames: it is now a
  solid dark strip with white text, a blinking dot and a bright progress line
  along its bottom edge.
- The collapsed left panel showed an empty square instead of the "+" button,
  and the collapse button took a whole row above the channels; it is now at
  the bottom of the panel.
- In the download window, wide gaps separated "Quality" and "Chat" from their
  lists; the labels now sit right next to them.
- The chat window of a recording without a chat file showed an empty search
  and graph with a small note at the edge; now it is a small "No chat"
  dialog that says where to download it, with "Stream summary" and "Close".
- For a recording joined with the stream beginning and stopped mid-stream,
  the chat window and the card took the chat length from the whole Twitch
  VOD rather than the recording: the graph was stretched and the end looked
  like a gap.
- In the bar of selected videos on a channel page the counter sat at the
  left edge and the options at the right, with a gap between them; now
  "Selected" and "Clear selection" are on the left, what and how to download
  next to them, and "Download" on the right. In a narrow window the labels
  wrap together with their lists.
- A chat file deleted or placed next to a recording showed up in the card
  menu only after switching tabs; the menu now checks the chat files on
  disk before opening.

## 0.13.0 — 2026-11-08

### Added

- **"Joining" tab in Downloads:** a downloaded stream beginning moves there
  right away, so it is clear the join is coming: "joining after the
  recording ends" while recording, "the recording is being processed,
  joining starts soon" after it, then the join itself with its progress.
  When the join is done, the row goes back to "Downloads".
- **The left panel collapses to avatars:** a button at its top; the choice is
  remembered between starts. Channel names show on hover.
- **Several viewers at once in the chat window:** clicking a name adds the
  viewer to "Viewers"; their messages show together, in time order. Names
  can also be typed in (several at once, separated by commas). Each chip has
  a × to remove the viewer, a click on the chips shows their messages again,
  "Whole chat" goes back to the feed.
- A chat taken from the chat archive is clearly marked as third-party: the
  menu item is "Get the chat from a third-party archive" with "logs.zonian.dev,
  a third-party service, not Twitch" under it, the Downloads row names the
  archive, and the chat window says the history came from it (a mark inside
  the chat file, so it stays after a restart; for a joined beginning, only
  the beginning's chat).
- Every Downloads tab shows how many rows it has, not only the running ones,
  and while something runs in a tab its name shimmers in the program's purple.

### Fixed

- A downloaded video without chat could get its chat only from its row in
  Downloads; the library card's menu now has "Get the chat" too.
- Library sorting "Newest first" went by the stream date, so a VOD from a week
  ago, downloaded just now, ended up far down. The default is now "Recently
  added first" (when the file appeared on disk); sorting by the stream date,
  newest or oldest, is still there.
- A downloaded stream beginning said "Done" while it was still waiting for
  the recording to end, and a join seemed to hang at 99%: after the video is
  joined the program checks its length, joins the chat and replaces the
  files, which now shows as its own stage ("video joined, checking and
  replacing files…").
- Statuses and log lines no longer speak in the first person ("downloading
  chat", "preparing to join…", "joined, waiting for the player to be
  closed" instead of "I'm …" forms in the Russian interface).
- The join is shown in the program's purple instead of teal.
- The download window had a lot of empty space under the frame: the title and
  details (as small chips) are now to the right of the frame, and the
  download choices span the whole width below.
- The bar of selected videos on a channel page moved up when there were few
  videos; it now stays at the bottom of the window.
- Chat window: the search field matches the other fields, with a search icon
  and a Ctrl+F hint, and Ctrl+F visibly puts the cursor there (the field was
  already focused on opening, so the shortcut seemed to do nothing). Going to
  a time is now labelled "Go to" with a button. The empty space between the
  time and the name is gone. "Summary" and "Open in the browser" moved next
  to the chat details.
- Library card: the join progress is no longer a badge at the top of the
  frame but a strip along its bottom, with the text on it.

## 0.12.0 — 2026-11-08

### Added

- **Actions menu on library cards:** the row of unlabelled icons on the frame
  is replaced by "Summary" and "⋯". The "⋯" button and a right-click open a
  menu with words and icons, in groups: watch in the window, open in the
  Windows player, summary · open the chat, open the chat video, get the chat
  from the platform, get the beginning's chat, make a video with chat on top ·
  convert, split into parts, show in folder · move to Recycle Bin. Getting a
  chat has a download icon, so it no longer looks like "open the chat".
- "Get the beginning's chat" is also in the library card's menu of a joined
  recording, not only in Downloads.
- **Chat window:** a chat is read right in the program, not in the browser.
  On top is a chat activity graph: spikes are marked, a click goes to that
  moment. The message feed with emotes and badges loads as you scroll both
  ways. You can search by word or by name, click a name to see all of that
  viewer's messages, and jump to a time (1:23:45). It opens from a chat card,
  "Read the chat" in a card's menu, the summary (where top chatters are
  clickable) and "Search all chats". The browser HTML stays as "Open the chat
  in the browser".
- **Chat from the chat archive (Twitch):** if the VOD of your recording is
  hidden or deleted on Twitch, "Get the chat from the chat archive" in the
  card's menu takes it from logs.zonian.dev, a public archive of Twitch
  chats, for the time of each part without chat (and for a joined stream
  beginning, only before the seam). It runs in Downloads with progress. The
  archive is not trusted: one fixed address, no redirects, size limits, and
  every line is parsed by the program's own IRC parser — lines of another
  channel or room, outside the recording or with broken tags are dropped.
  When the archive cannot help, the item is greyed out with the reason under
  it (not Twitch, the channel is not logged, no chat for that day, the chat
  is already there). Big chats are fine: messages go to disk in 16 MB
  chunks, up to 3 GB per day (about 7–8 million messages), and a day is
  downloaded once for all parts of a recording. Kick, YouTube and VK Video
  Live have no such archives.
- **"What to download":** video · sound only · chat only — a switch above
  the quality in the download window and in the bar of selected videos.
  The quality list has resolutions only; with "chat only" the chat choice has
  no "no chat"; for a video without chat "chat only" is greyed out with the
  reason.
- **"Chats" tab in the library:** chats downloaded without video are listed
  separately. Their card shows the chat itself instead of a stream frame and
  length: an activity graph and the number of messages; viewers and the
  chat's length are in the tooltip.

### Fixed

- A recording split twice into different numbers of parts (into 2, then the
  whole into 3) showed as one card "5 parts", and the player mixed the
  pieces of both splits. Each split is now its own card.
- Watching a recording in progress: "To live" seemed to jump to the middle
  of a short recording. Playback stays 12 seconds behind the recorded edge
  (the newest seconds are still being written), and the timeline used to
  show those seconds too. Now the timeline ends where watching is possible,
  so "To live" lands at its end, and the length is asked every 5 seconds
  instead of 8, so the edge moves more smoothly.
- Turning a live chat into .chat.json (all platforms) held every message in
  memory at once — gigabytes for a day-long stream of a big channel. It now
  reads the file in passes and writes messages one by one.
- "Only a section" in the download window: the "to" field wrapped onto its
  own line. "From … to …" is one line again.
- A Twitch chat download looked as if it jumped back to 0% or hung: after the
  messages TwitchDownloader fetches emotes and badges, embeds them and adds
  viewer details, each stage counting from zero. The Downloads row now names
  the stage ("downloading chat", "getting emotes and badges", "embedding
  emotes", …), and "preparing chat for the browser" shows its step out of
  three.
- "Get the beginning's chat" made an HTML (and a chat video) for the
  beginning's chat that was deleted right after, and the final HTML of the
  whole chat showed no progress. Now the HTML is made once, for the whole
  chat, with progress; the same for a chat fetched for your own recording.
- The row of a joined stream beginning lost its picture once the stream had
  ended; it now shows a frame from the recording.

## 0.11.0 — 2026-11-08

### Added

- **Chat only, no video:** a new choice in the quality list when downloading a
  stream recording or a clip (one video or several selected on the channel
  page). Only .chat.json and .chat.html are saved; videos without a chat are
  skipped.
- **The chat of a stream beginning can be fetched after joining:** "Get the
  beginning's chat" on its Downloads row. The chat goes into the recording's
  chat before the seam; the live chat stays.
- **Chat for your own recording** if the chat was off while recording: "Get the
  chat from the platform" on the library card finds the platform's recording
  of that stream (by time, then by title) and gets the chat of exactly the
  recorded stretch, for each part without a chat.
- **A chat downloaded without video is in the library** as a "chat" card:
  a click opens its summary (messages, viewers, most active, emotes, peaks),
  with "Open chat" and search by word or name. "Search chats" finds it too.

### Fixed

- "Get chat" on a stream beginning that was already joined with the recording
  would have written the beginning's chat over the recording's live chat.
- The "Editing" tab in Downloads is always there; when it is empty it says
  what appears in it. Before, it disappeared after a restart.

## 0.10.0 — 2026-11-08

### Added

- **Back to defaults:** "Default recording settings" in the Live recording
  section of the settings (everything there except the folder), and "Same as
  general settings" in a channel's recording settings. Both only fill in the
  fields: nothing changes until "Save".
- **Joining is easy to see:** a bright badge that fills up on the library
  card with a bar along the frame, and its own line with a bar and time left
  in Downloads. While the joined file waits for a player to let go of the
  recording, the line says so.
- A download row is laid out in parts: "Done · size", the joining result as a
  label, and the chat note on its own muted line. A stream beginning without
  a platform picture shows a frame of the file (or of the live stream).
- While the timeline frames are being made, the player shows how far along
  they are: "frames are being made… 40%".

### Fixed

- Joining percentages no longer "stick" at about 85%: the second pass, which
  rewrites everything, now gets its fair share.
- **A recording that is still being written could not be watched in the
  window** (and "To live" did nothing) when Twitch sends the stream as MP4
  pieces inside .ts: ffmpeg waited for the end of the growing file. The
  program now finds the place itself (a fragment starting with a key frame)
  and feeds the file to ffmpeg as it grows.
- The page title row (Library, Downloads, Log) is even: the note "6 files · 12 GB"
  sits on the title's line, and the buttons are centred on it.
- A cancelled, failed or finished row in "Editing" can be removed: a cross
  on the row and "Clear list" for the whole tab (the files stay).

## 0.9.0 — 2026-11-07

### Added

- **The channel name is a link to its page** — on library cards, in
  Downloads, in "Recording now" and in the player.
- **Joining status on the library card:** "joining 45%" while the stream
  beginning is joined with the recording, "not joined" with the reason in
  the tooltip. Repacking a recording before the join is shown too
  ("preparing the recording for joining…").
- **Downloads has tabs:** "Recordings" (running recordings), "Downloads"
  (what is being downloaded) and "Editing" (when there is any), with
  counters. A recording row also shows how its stream beginning is going.
  Its buttons have labels: "Play" and a red "Stop".
- **The whole recording kept after "Split" can be split again** into other
  pieces: the scissors are on its card too. The new pieces go to the channel
  folder next to the earlier ones, and the whole one stays in "whole" (or
  goes to the Recycle Bin when the checkbox is off).
- **Conversion from the library is shown in "Editing"** (Downloads): a row
  "converting to mkv" with the time it has been running, then "Done" with
  "Open" and the folder. The notice about a started split or conversion
  opens Downloads right on this tab.

### Fixed

- **A Twitch recording made of MP4 pieces inside .ts** (Twitch sometimes
  sends a stream like that) did not play in Windows Media Player, and
  splitting it failed with "Conversion failed!" on its broken last frame.
  Such a recording is now turned into a real .ts (or MP4 with conversion
  on) right after the stream; splitting and joining skip the broken frame
  of recordings made earlier.
- The notice about a stream beginning that did not download shows the same
  short reason as its row, not the network error.
- **Splitting a recording open in the new Windows Media Player** is refused
  again. The check missed this player and itself made the player drop the
  file ("can't access local storage"), so the split went ahead. Now the
  check sees the player without disturbing it, and refuses before the split
  starts, not after it.
- **"Convert" refuses a recording open in a player** (it deletes the .ts
  afterwards), like splitting does.
- **The "Convert" window of the whole recording** no longer offers pieces
  it never made: it only converts, and splitting is done with the scissors.
- While the frames over the timeline are still being made (about a minute
  for a long recording), the player says "frames are being made…" instead
  of showing only the time.

## 0.8.0 — 2026-11-07

### Added

- **New versions of yt-dlp and TwitchDownloader are found by themselves** —
  at startup and every 12 hours, like updates of the program. A button at
  the top ("Update yt-dlp …") opens the update. The "Not downloading?"
  banner in Downloads is gone.

### Changed

- **The "Recording now" row** has no moving bar any more (it looked like
  waiting): "REC" with a blinking dot on the frame and figures that grow —
  recorded, size, speed, chat messages.
- A channel kept only for its own recording settings (not a favorite, no
  auto-record) no longer shows on "Live" as "live, not recording".

### Fixed

- **The stream beginning ended before the recording started**, so there was
  nothing to join on: Twitch publishes the recording of a live stream with
  a delay, and the beginning was taken too early. Now the program waits
  until the platform's recording reaches the start of the recording.
- **Joining a Twitch recording made of MP4 pieces inside .ts** lost its end
  (12 minutes out of 45) and the join was discarded. Such a recording is
  now repacked into MP4 first; the joined file is MP4.

## 0.7.0 — 2026-11-07

### Added

- **"Recording now" in Downloads:** every running recording as a row — the
  stream, quality, part, how long and how much is recorded, recording speed
  and chat messages, with Play, Show in folder and Stop. In the sidebar a
  recorded channel shows a red dot and the recording time instead of
  "live".

### Changed

- "Parallel recording pieces" is now called "Parallel fragments" and is 8
  by default instead of 4: on a Twitch recording 4 gave about 17 MB/s and 8
  about 19 MB/s, more did not help. Settings already saved keep their
  value. The setting has a "?" tip with this measurement.
- **New icon:** a silver wheel with a play button on black — in the window,
  the tray, the program and installer icons. The red recording dot is shown
  in the tray only while recording.

### Fixed

- **The Twitch and Kick stream beginning downloaded slowly** — tens to
  hundreds of KB/s: ffmpeg fetched the recording one piece at a time. Now
  the program downloads it itself, several pieces at once (as many as
  "Parallel fragments" in the settings): on the same recording,
  about 14 MB/s instead of 30–250 KB/s. This applies to VK as well.
- **Joining the stream beginning with the recording** could add a frame at
  the seam, and with an MP4 recording mix up the frames of the beginning
  and the recording: the time scales of the two did not match. Now the
  join is frame-exact.
- The stream beginning row shows a short reason instead of the yt-dlp
  error ("the stream's recording is for channel subscribers only"); the
  error text is in the log. Such lasting reasons are no longer retried.
- The search box at the top had a border and rounded corners on its right
  side, so the search button stuck to it from the outside. The box and the
  button are one control again and light up together on hover and focus.

## 0.6.0 — 2026-11-07

### Added

- Updates are checked not only at startup but also every 12 hours while the
  program runs: if it stays in the tray for days, the "Update" button still
  appears without a restart.

### Changed

- **"Split into pieces" keeps the whole recording** by default: it moves
  with its chat to the `whole` folder next to the pieces and stays a
  separate card in the library. Untick "Keep the whole recording" to send
  it to the Recycle Bin as before.
- The progress line of splitting shows the percentage and the time left;
  edit rows in "Downloads" show a frame of the recording.

### Fixed

- **Splitting or joining a recording open in the new Windows Media Player**
  cut the playback off: that player lets Windows move an open file, so the
  recording vanished from under it. Now the program asks Windows who holds
  the file and waits, naming the program ("open in «Media Player»").

## 0.5.0 — 2026-11-07

### Added

- **A stream beginning that failed to download is retried by itself**:
  after 1, 3 and 5 minutes. The row in "Downloads" shows when the next try
  is and why the last one failed. "Retry" still starts it right away.
- **"Split into pieces" in the library** for any finished recording or
  download, in `.ts`, MP4 or another format: by size, by duration or into
  equal parts, without re-encoding. The pieces keep the format, each gets
  its own chat, and they play one after another as one card; the whole file
  goes to the Recycle Bin. Pieces of a split download now also form one card.
- **Conversion to MKV or sound only.** Besides MP4 a recording can now be
  converted to MKV (survives a crash better) or to sound only, M4A (dozens
  of times smaller, for podcasts and music streams) — in the settings, for
  each channel and in the "Convert" dialog in the library. Still without
  re-encoding; sound that does not fit into M4A as is is re-encoded to AAC.
  The stream beginning is joined with such recordings too: with a sound-only
  one the seam is found by the sound.

### Fixed

- After "Stop recording" processing could wait two minutes if the file was
  briefly open in a player or the antivirus. Now it tries again in seconds.
- **Joining the stream beginning while you watched the recording** failed
  and started over from scratch every minute while the file stayed open.
  Now it waits for the file to be closed and finishes the join.
- "Open" and "Show in folder" for a file with a comma in its name (and no
  spaces) opened a different folder.
- Downloading the YouTube and VK stream beginning pauses briefly between
  attempts after a dropped connection instead of retrying at once.
- Converting a `.ts` piece of an already split recording no longer splits it
  again, and the piece length fields in the dialog no longer accept values
  the program rejects.

## 0.4.1 — 2026-11-01

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
