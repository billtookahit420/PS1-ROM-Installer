# Recomp ROM Installer — PSX support build spec

Status: BUILT 2026-11-15 in a live turn (Duane's go, same night),
verified against synthetic fixtures (22/22) and real archive sets
on recomp-hq (Tekken 3 single-disc multi-track; Alone in the Dark:
The New Nightmare 2-disc). NOT yet published — release awaits
Duane's word. The multi-disc OPEN question below is resolved:
multi-disc packages record `discs = [...]` (expected cue names)
and `disc_serials = [...]` (expected serials) in game.toml [game],
verified on Alone in the Dark: The New Nightmare, Parasite Eve II,
Fear Effect, and The Legend of Dragoon. One ground-truth surprise
the build had to absorb: Tekken 3's boot EXE lives in a subfolder
(TEKKEN3\SLUS_004.02), not the disc root — the serial reader scans
one level of subdirectories and parses SYSTEM.CNF's BOOT path.

(was: AUTHORIZED by Duane 2026-11-13 ("build away"), NOT built.
Two attempts to hand the build to a helper were refused by a safety
screen before any work happened (2026-11-13 ~08:07 UTC). Duane is
hand-copying PSX bin/cues in the meantime. This spec exists so the
next attempt — in a live turn, not via a helper handoff — starts
from verified package ground truth instead of chat memory.)

## Diagnosis (verified in source, 2026-11-13; re-verified 2026-11-13 PM)

The installer never had PSX support — it excludes PSX by design:

- `installer.py` header, "Package ground truth": "PSX — Disc images
  ship inside the package — never flagged."
- `installer.py` `_detect_unit()`: a folder whose `game.toml` has a
  `[game] id` and matches no earlier system returns `(None, "psx")`
  — the package is filed as ignored, its folders never inspected.
- No "psx" entry in `SYSTEMS`; `.cue`/`.bin` absent from
  `ALL_ROM_EXTS`.
- Control tests on a fresh Tekken 3 GuardFix2 unzip (done 2026-11-13):
  removing the guard-staged boot EXE changed nothing; adding dummy
  .cue/.bin changed nothing. The guard fold-in (Recomp Tools v1.0.22)
  is NOT the cause — the earlier staged-EXE suspicion was wrong and
  was owned to Duane.

That written premise was never true for Duane's fleet: his PSX
packages ship WITHOUT disc images; he copies bin/cues in by hand.

## Package ground truth (checked 2026-11-13 against real packages on this VM)

Single-disc packages (`~/workspace/stutter/guardfix/control/Tekken3/`,
`~/workspace/stutter/gt-respin-verify/GranTurismo/`):

- `game.toml` `[game]` carries the identity:
  - `id = "SLUS-00402"` (serial, hyphenated)
  - `exe = "disc/SLUS_004.02"` (boot EXE path inside the disc image;
    underscore/dot form of the serial)
  - `name`, `players`
- `disc.cfg` holds the disc path the launcher/runtime uses — in
  conversion/verify copies it is an absolute source path from build
  time (e.g. `/home/hatch/workspace/tekken3-recomp/Tekken-3/prepared_disc/game.cue`),
  so it is NOT a reliable expected-filename source. Package READMEs
  state: "No disc is included in this package. Use your own legally
  dumped disc image: pick the .cue in the launcher (Browse For Disc)
  or run with --disc <your .cue>."
- Guard-era packages (Recomp Tools v1.0.22+) stage `disc/<boot EXE>`
  inside the package. Therefore a presence check must look for an
  actual `.cue` (the install destination state), never for "disc/
  folder is non-empty" — the staged EXE would false-positive it.

## Build outline (v1.1 scale — a feature, not a patch)

1. Add "psx" to `SYSTEMS` with exts {".cue"} (the cue is the install
   unit's anchor; bins follow the cue's FILE entries).
2. `_detect_unit()`: PSX packages become real units instead of
   `(None, "psx")`. Identity = `[game] id` serial + boot EXE name
   from `[game] exe`.
3. Presence: package needs install iff no `.cue` is present at the
   package's disc location. Staged `disc/<EXE>` alone = still needs
   its disc.
4. Source indexing: index complete cue/bin sets from Duane's source
   folders, loose and inside .zip archives (all tracks a cue names;
   an incomplete set is reported, never half-installed).
5. Candidate verification by serial before installing: the set's
   boot EXE filename (from the cue's data track / SYSTEM.CNF on the
   disc) must match the package's expected serial. Wrong-region /
   wrong-title dumps with plausible filenames must be rejected —
   never fill by filename alone (standing hard rule of this tool).
6. Install is a complete-set, atomic-per-file copy using the tool's
   existing temp-file + os.replace discipline; multi-disc sets
   install all discs or none.
7. After a fill, point `disc.cfg` at the installed .cue (the PSX
   analogue of the existing rom.cfg fix-up in ROM_CFG_SYSTEMS).

## OPEN — must check before coding the multi-disc branch

No game.toml on this VM lists multiple discs: a workspace-wide grep
for `disc_serials` / `discs =` in game.toml files found zero hits
(2026-11-13). How a multi-disc single package (Fear Effect,
Parasite Eve II, Alone in the Dark: The New Nightmare) records its
disc set is NOT verified. First step of the build: read one real
multi-disc package's game.toml + disc.cfg on HQ / the server library
and pin the format here. (Background runs cannot SSH to HQ — egress
denied 2026-11-13; this check belongs to a live turn, and Duane's
2026-11-12 diagnostic latitude covers reading archive copies.)

## Acceptance tests

- Duane's exact repro: fresh-unzipped PSX package, no bin/cue
  anywhere → flagged as needing its disc (today: "no roms needing
  to be installed").
- Fresh Tekken 3 package + correct archive set in source → installs,
  serial-verified; package then scans as satisfied.
- Guard-era package with only `disc/<EXE>` staged → still flagged.
- Wrong-serial set with a matching filename → rejected, reported.
- Multi-disc package → complete set installs; a source missing one
  disc reports incomplete and installs nothing.
- Zipped source set → matched in place, only the set extracted.
- Existing suites (NES/GB/SNES/GBA/N64/VB/SMS/GG) unchanged and green;
  the 36/36 stress discipline from v1.0.0 applies to the new code.

## Publishing

Unchanged standing rules: no publish without Duane's word; a release
means tag AND formal GitHub Release; packages never contain ROMs,
discs, or BIOS.
