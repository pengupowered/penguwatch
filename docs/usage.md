# Using PenguWatch

The dashboard is a single page with tabs. Every tab has an address (`#/upnext`, `#/optimizer`, `#/system`, ...), and
`g` followed by `u o l r d p c s i h y e` (in the order of the tabs: Up Next, Optimizer, Library, Ratings, Discover, Pick, Schedule, Services, Insights, History, System, Settings) jumps to one; `/` filters the library and `?` lists the shortcuts.

## Up Next

Every show you're watching with its exact next episode (`S02E05 – Title`), a progress bar and episodes left. **Watched**
checks it off and sends that exact episode to your library service (SIMKL by default; nothing is sent with the local library). Filter to the services you have right now, to episodes
that aired today, or (with two people in the household) to titles you both want.

## Library and episode tracking

* Season-by-season **episode checklist** for every title (titles and air dates from the catalogue drivers: SIMKL or TVmaze, as configured). Mark single episodes,
  whole seasons or "first N episodes" watched or unwatched; only the episodes that actually changed are sent to the account.
  The +1 / −1 episode and ± season steppers use the real season lengths.
* Movies are synced too (plan to watch → watched).
* **English first:** where the catalogue knows an English title that differs from the stored one (many anime are stored under
  their romaji title, such as "Sousou no Frieren"), the English title is the main one, with the stored title in small
  text underneath. The calendar feed reads "Frieren: Beyond Journey's End (Sousou no Frieren) S01E05".
* **Forgiving search:** accents, case, punctuation and long vowels are ignored. "shogun", "shougun" and "shoogun" all
  find *Shōgun*; "pokemon" finds *Pokémon*; "rezero" finds *Re:Zero*. The search covers the stored title, the English
  title, the catalogue's alternative titles, the network and genres.
* **Likely interest:** sort the library by a 0-100 score built from the rating (with shrinkage for few votes), your
  genre taste (what you finished or rated) and household signals; the badge's tooltip says why.
* Per title: status, your rating, priority, notes, who wants to watch it (with two people), and the
  service it streams on. Household fields are never overwritten by a sync.
* **Your own ratings:** add rating sources (Letterboxd, a spreadsheet, a friend) and import their values; sort and
  filter the library by them, see them as badges on the cards and on a title's details. The **Ratings** tab (`g` then `r`)
  lists the sources, creates and edits them and imports a CSV file (read in your browser, in batches; the rows that
  did not match are listed and can be downloaded). See
  [Extending](extending.md#bring-your-own-ratings).
* The library draws the first 60 cards and loads more as you scroll; there is a **Compact** view.

## Discover

Discover needs a driver that offers `discovery` (SIMKL does; TVmaze does not, so Discover is off unless a driver that offers it is configured). The lists below are SIMKL's, the default.

* **Trending** TV, movies and anime (today / week / month) from the discovery driver, marked with what's already in your library and
  which of your services carries each title.
* **For You:** "because you liked X & Y" picks from the discovery driver's recommendation lists (SIMKL's "users also watched" and "similar"), seeded by your
  highest-rated titles and those you both want.
* One click adds a title to your library *and*, when the library has an account, to that account's plan-to-watch list.

## Optimizer and rotation planner

* Each service is ranked by backlog hours, ready-to-binge seasons, titles you both want and ratings, with a
  keep / cancel / rotate-to verdict, "wait for the finale" detection and savings tracking.
* **Bundles:** a service can include other services (see [Configuration](configuration.md#services-and-ratings)). While a bundle
  is active its members count as covered and cost nothing extra. The optimizer suggests switching to a bundle when it
  is cheaper than its members paid separately, and dropping one when only some members are watched.
* **Rotation planner:** plans the next 3, 6 or 12 months: which service to keep, subscribe to, pause or cancel
  (cancelling at the renewal when it is known), within `max_active_services`, at your 90-day viewing pace. A bundle is
  planned as one service and never alongside its own members; free services are always kept. Backlog hours are
  weighted (watching 1.0, plan to watch 0.5, on hold 0.25, high priority or both want 1.0) so one 900-hour
  plan-to-watch long-runner does not dominate. A table view sits behind the timeline.
* **Value report:** hours watched, cost per hour and a cancel/keep verdict per active service. Enter each service's
  renewal day under *Services & Sync* to get cancel-before-renewal advice and alerts. Each service keeps its price
  history.
* **Tidy** lists titles with no service (most viewed first), shows that came back with new episodes, and long-stalled
  ones.

## Tonight's Pick

A weighted random pick filtered by service availability, audience, rating, binge-readiness and show or movie.
Caught-up shows are skipped.

## Air schedule and calendar feed

Upcoming episodes for the titles you track, plus a subscribable **iCal feed** for your phone, Google or Outlook
calendar. The feed also has an all-day event for each active paid service's next renewal (monthly or yearly) and a
"cancel by" reminder the day before when the optimizer says the service is not worth keeping. Event ids are stable,
so calendars update them in place. With an access token set, the feed URL shown on the Air Schedule tab carries its
own key, because calendar apps cannot sign in.

## Insights

Monthly hours, hours by service, top genres and your weekday rhythm.

## History

What you watched over the last 7 days, 30 days or 12 months, each compared with the period of the same length just
before it (an arrow and a percentage, or "new" when there was nothing before). The periods roll: they are whole days
ending today, not calendar weeks, months or years. It shows hours, episodes, movies, titles started (the first episode
you ever watched falls in the period) and finished (completed, last watched in the period), days with viewing, an hours
per day (or per month) chart, top shows, hours and cost per hour by service, top genres, and when you watch by weekday
(Sunday first, like Insights) and hour of day. Days and hours are in your configured time zone. A watch with only a date
(no time) counts for its day but not for the hour-of-day chart. Your last choice of period is remembered in that browser.

- **Bulk marks.** Three or more episodes with exactly the same timestamp (marking a whole season, "first N", an import)
  say nothing about when you watched, so they are not counted in any figure or chart; the page says how many were left
  out and offers to include them.
- **Cost per hour.** PenguWatch does not record when you subscribed to a service, only whether it is active now and
  every price it has had. So the cost is worked out for services that are active now, as if paid for every day of the
  period at the price in effect that day, divided by the hours watched on the service. Cancelled services show hours only.

## Settings

The **Settings** tab holds everything you can tune: household names, the sync schedule, the optimizer's weights, the daily
digest and event webhooks, the theme and time zone, which services fill each data role, and the advanced values. Change a
value and save; most apply at once, and a few say "applies after a restart". Reset puts a setting back to its default,
**Versions** lists the last 50 saves and restores one, and Export and Import move the settings as a YAML file. A setting
that an environment variable fixes shows "From environment" and cannot be edited there. Every setting, its range and its
default is in [Configuration](configuration.md#all-settings). The tab is the last one (`g` then `e`); nothing changes
until **Save**, and leaving the tab with unsaved changes asks first.

## On a phone

Under 720 px wide the page switches to a bottom tab bar (Up Next, Plan, Library, More), a one-line header, filter chips
with counts and thumb-sized check buttons.

## Light and dark

The page follows your device's light or dark setting until you pick one with the sun/moon button in the header; the
pick is kept in that browser.

To install it as an app, open it in Chrome or Safari and choose **Add to Home Screen** (Chrome: *Install app*). It
opens full screen with its own icon. A service worker keeps the static page shell so the app starts fast; your data is
always fetched live and never cached. Over plain `http://` on a LAN address some browsers offer only a shortcut rather
than a full install, because service workers need a secure origin (`localhost` or HTTPS).
