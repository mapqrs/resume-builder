# Sample fixtures

Drop-in starter files for the resume builder. Use these to:

1. **Try the tool end-to-end without typing your own resume yet.** Copy
   `master.example.yaml` to `master.yaml` in the project root, then run
   `./run-web.sh` and follow the tailor.
2. **See what a "good" master looks like.** The example is India-first
   (CGPA, INR-aware, Indian companies + universities) but the structure
   is universal — adapt to your own context.
3. **Test a JD against the example master.** Upload `jd.example.txt` in
   the web UI's JD field, or pass `--jd samples/jd.example.txt` on the
   CLI.

Both files are deliberately bland — placeholder content, no real
personally-identifying information. Replace before submitting anywhere.

## Files

| File | What it is |
|---|---|
| `master.example.yaml` | A complete `master.yaml` with all the fields the schema supports. |
| `jd.example.txt` | A realistic India-tech JD (Senior Backend Engineer, payments). |
