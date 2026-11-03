# Running a side-by-side test (checklist)

The platform records what each monitor showed. Whether that data supports a white paper depends on how
the test is set up. This checklist covers both.

## Methodology decisions (decide before the first test)

- **Agreement is not accuracy.** Consumer monitors agreeing with each other doesn't show any of them is right:
  many share the same sensor family. To claim your monitor is *accurate*, run at least part of the test next to a
  **reference-grade instrument** (rent one, or co-locate near a government monitoring station) and import its
  data as reference data.
- **Follow a published protocol** so reviewers accept the method, for example:
  - US EPA, *Performance Testing Protocols, Metrics, and Target Values for Fine Particulate Matter Air Sensors* (2021);
  - South Coast AQMD's AQ-SPEC field evaluation approach.

  The usual metrics are R², slope and intercept, RMSE, mean bias, unit-to-unit precision, and data completeness.
- **Cover a range of concentrations.** In clean indoor air every device reads low and they agree trivially.
  Plan controlled events (cooking, incense, opening a window) and note their times in the run notes.
- **Test several units of each model** where you can, to report precision between units.
- **Put one of your own monitors in a camera's view as well**, and mark it "This is one of our own monitors"
  with its cloud ID. Its camera-read values can then be compared with what it sent to your cloud. That
  measures the OCR's own error rate, which belongs in the methods section.

## Bench setup

- [ ] Monitors side by side at the same height, same airflow, away from vents, doors and windows (unless that's the test), not touching each other.
- [ ] Each monitor powered on for its stated warm-up time before the run starts.
- [ ] **Turn off auto-dimming, screen sleep and screen cycling** on every monitor where possible. Note which monitors cycle screens. This version reads fixed screens only.
- [ ] Camera on a tripod or clamp. It must not move during the run; if it does, re-mark the corners.
- [ ] Constant lighting: a desk lamp that stays on day and night, angled so there's **no glare** on the screens. Day/night changes are the main cause of misreads.
- [ ] Every number at least ~25 px tall in the picture. Check with `python -m aqagent snapshot`.
- [ ] Focus locked (`autofocus: false` in the agent config); exposure locked if the image pumps.
- [ ] Capture computer: power settings "never sleep", plugged in, Windows Update paused for the test period, agent installed to start automatically (see agent/README.md).
- [ ] Clock: automatic time sync on. `python -m aqagent check` prints the difference from the server; keep it under 1–2 s.

## Starting a run

1. Open **Calibrate**. Mark each monitor and its numbers, press **Test reading**, and fix anything marked "would need review".
2. Set realistic plausible ranges per number (e.g. PM2.5 0–1000), so wild misreads go to review.
   Optionally tick **Store only the marked monitors** for each camera, but only once every monitor in its view is marked.
   Anything not marked is blacked out and can't be recovered.
3. On **Data & exports**, start a **test run**. Put the details in the notes: room, device models, firmware versions, serial numbers, positions, anything unusual.
4. Check the **Live** page: every camera online, readings updating, "stills received" near 100%.

## During the run

- Check the Live page once or twice a day, or set `ALERT_WEBHOOK_URL` to be messaged when something breaks.
- Work through the **Review** queue regularly. A few minutes a day beats hundreds of items at the end.
- Note any interventions (doors opened, monitor moved, power cut) in the run notes with the time.
- If an alert says a camera view **shifted**, readings are being corrected automatically. Check that nothing else
  changed (e.g. a monitor was picked up and put back), then press **Accept new position** on Calibrate. If a monitor
  is **not found**, its readings go to review until you fix the setup or re-mark its corners.
- **Lined up → Go back in time** is the quickest way to scan a long test or show someone exactly what happened at a given moment.

## Ending the run

1. **Stop** the run on the Data page.
2. Clear the review queue for the run period.
3. Export the **readings CSV**, the **pictures manifest** and the **reference CSV** for the run, and keep them with the backup.
4. Open **Results** for the run. Choose the reading, the averaging period (1 hour and 24 hours are usual for PM2.5)
   and the reference. You get:
   - the statistics table, time series and scatter plots;
   - reading quality, and the own-monitor accuracy check;
   - a draft methods paragraph.

   Download the CSVs, or print the page to PDF. For a figure of the setup, use **Lined up → Download as image**.
