# Tampa Bay Networking Calendar

One Outlook calendar that merges events from your Top 15 networking organizations.
It rebuilds itself every morning on GitHub (free), and Outlook picks up the changes.

## One-time setup (about 15 minutes, all in a web browser)

1. **Create a free GitHub account** at github.com.
2. **Create a repository**: click **+** (top right) > **New repository**.
   Name it `tb-networking-calendar`, set it to **Public**, and click **Create repository**.
   (Public is required for free GitHub Pages hosting. Nothing private is in it: just public event listings.)
3. **Upload the files**: on the new repo page, click **uploading an existing file**.
   Unzip this package and drag in *everything inside the folder*, including the hidden
   `.github` folder. (On a Mac press Cmd+Shift+. in Finder to show hidden folders. On Windows,
   View > Show > Hidden items.) Click **Commit changes**.
   If the `.github` folder will not drag in: click **Add file > Create new file**, type
   `.github/workflows/build.yml` as the name, paste the contents of that file, and commit.
4. **Turn on hosting**: Settings > **Pages** > Source: *Deploy from a branch* >
   Branch: `main`, folder: `/docs` > **Save**.
5. **Run it the first time**: **Actions** tab > enable workflows if asked >
   *Build networking calendar* > **Run workflow**. Wait about 2 minutes for the green check.
6. **Check the health report**: `https://YOUR-USERNAME.github.io/tb-networking-calendar/status.html`
   It shows how many events came from each organization and flags any that failed.
7. **Subscribe in Outlook** (web or new Outlook): Calendar > **Add calendar** >
   **Subscribe from web** > paste
   `https://YOUR-USERNAME.github.io/tb-networking-calendar/networking.ics`
   > name it "Networking" > pick a color > **Import**.
   Classic desktop Outlook: Home > Add Calendar > From Internet.

After that it runs daily on its own. Outlook refreshes subscribed calendars on Microsoft's
schedule, usually every few hours, so a new event can take up to a day to appear.

## How events look

- Titles are tagged by group: `[ABC] Chili Cook-off`, `[CREW] Monthly Luncheon`.
- Each event links to its registration page in the event notes.
- Events are marked *Free*, so they never block your availability for meetings.
- Past events drop off after 30 days.

## Day-to-day

- **Add an event by hand** (groups with no public calendar, like Contractors, Closers &
  Connections, or sites that block automated reading, like FGCAR and the Tampa Bay Chamber): edit `manual_events.yaml` on GitHub (pencil icon), copy the example block,
  and commit.
- **Drop a type of event** (for example, classes): add words to that group's
  `exclude_title_regex` in `sources.yaml`.
- **Pause a group**: add `disabled: true` under it in `sources.yaml`.
- **Add another group from the 111-org list**: copy a block in `sources.yaml` that uses the
  same website platform, then change the name and URLs.
- **Refresh now**: Actions > Build networking calendar > Run workflow.

## When a group's website changes

If a group redesigns its site, its row on the status page turns orange or red. The calendar
keeps showing that group's last good events for up to 3 weeks, so nothing disappears
overnight. Usually the fix is a one-line change to that group's `link_regex` in `sources.yaml`.

## Files

| File | Purpose |
| --- | --- |
| `sources.yaml` | The organizations, how each one is read, and filters |
| `manual_events.yaml` | Events you add yourself |
| `build_calendar.py` | The script that builds the calendar |
| `.github/workflows/build.yml` | Runs the script daily on GitHub |
| `docs/` | Output: `networking.ics` (the feed) and `status.html` (health report) |
| `tests/test_parsers.py` | Checks that the date readers handle each site's format |
