# Recomp ROM Installer

Project home: https://github.com/billtookahit420/Recomp-Rom-Installer

A small utility for the recomp converter family: it scans your
extracted recomp game packages, finds the ones that are still waiting
on a player-supplied ROM, and fills in the exact ROM each one was
built from - matched by **hash**, never by filename alone - from ROM
folders or `.zip` sets you point it at (a hard-drive folder, or your
home server mounted as a share).

## Run it

- Linux: `./recomp-rom-installer.sh`
- Windows: `recomp-rom-installer.bat`

Both launchers create a local `.venv` and install PySide6 on first
run. Python 3.10+ recommended.

## What it does

1. **Scan packages** - pick the folder your extracted recomp packages
   live in. Every package waiting on a ROM is listed with the identity
   it expects (taken from the package itself: its README / play
   script / `game.toml`). The scan is a listing only - it never reads
   the game executables, so even a library of hundreds of packages
   scans in seconds. For NES / GB / SNES packages the known-ROM hash
   lives inside the executable; those are checked when you install,
   per game, and the result is cached (in a `cache/` folder beside
   the app) so a repeat run doesn't read them again.
2. **Add sources** - one or more folders of loose ROMs and/or `.zip`
   ROM sets. Zips are searched *in place*: entry CRCs pre-filter
   candidates and only those entries are hash-checked - the archive
   is never unpacked wholesale.
3. **Find ROMs + install** - each package gets the one ROM whose
   hashes match *all* of its identity data. The ROM is copied to a
   temp file inside the package, hash-verified there, and only then
   moved into place - an interrupted install never leaves a partial
   file behind. For the launcher-based families (NES / GB / SNES /
   GBA) the package's `rom.cfg` is pointed at the installed ROM, the
   same thing the game's launcher does when you pick the ROM by hand.

## Statuses, honestly

- **waiting** - no ROM in place yet; for NES / GB / SNES packages the
  identity check happens at install time, so this is the honest
  pre-install state.
- **present, unverified** - a ROM file is already in the package, but
  its identity is only verified at install time.
- **filled** - the exact ROM was found and installed.
- **already present** - the correct ROM is already in the package.
- **not found** - no source ROM matched the package's hashes. A
  same-name dump from the wrong region is *not* a match and will
  land here, by design.
- **conflict** - a different ROM file already sits in the package.
  It is never overwritten unless you tick that row's **Replace**
  box and run again - that is a deliberate per-package action.
- **no identity data** - the package carries no usable hash identity,
  so nothing can be matched safely. It is left alone rather than
  guessed at.
- **error** - e.g. a read-only destination folder; the row says why,
  and the other packages still run.

## Which packages it services

NES, Game Boy / Color, SNES, Game Boy Advance (including the
per-OS `linux/` and `windows/` payload folders inside a GBA package),
Nintendo 64 (Majora's Mask - the one title the N64 converter
supports), Virtual Boy, Master System and Game Gear, and
PlayStation.

PlayStation packages ship *without* their disc images. The installer
reads the expected disc serial(s) from the package's own
`game.toml`, finds complete `.cue`/`.bin` sets in your source
folders (loose or inside `.zip` archives), and verifies each set by
the serial read off the disc image itself - never by filename
alone. Multi-disc games install every disc or none, and after a
fill the package's `disc.cfg` points at disc 1. Genesis packages
pick their ROM in their own launcher and are not flagged. The GBA
*BIOS* is not installed by this tool: the game's own launcher asks
for it once, as before.

## Platform notes

One cross-platform Python codebase for Windows and Linux. Atomic
install uses a same-folder temp file + rename on both. Windows was
verified by code review only (no Windows machine in the build
environment) - the same honesty standard as the family's
compile-verified Windows builds.

## Credits

Built for the recomp converter family. Frameworks by Matthew Stanley
(mstan) / RetroPortingToolkit (retroportingtoolkit.com;
github.com/mstan, github.com/RetroPortingToolKit); N64Recomp by
Wiseguy. This tool ships no ROMs, no BIOS files and no game data -
it only moves your own dumps into packages you already own.

## Credits

The game packages this installer serves are built with **Matthew Stanley's (mstan) RetroPortingToolkit** recomp frameworks. Disc/ROM images are the user's own; nothing copyrighted is included or distributed.
