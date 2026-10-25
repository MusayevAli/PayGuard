# Hardware recovery status

**R2 is quarantined for a gate-level reset fault.** Its implemented active-low
auxiliary reset is tied low. R3 and R4 are preserved historical overlay attempts;
neither can repair that bitstream. Do not execute their old loading/probing
instructions against R2.

The current path is a separately built R5 image using the existing keys, followed
by a new private USB transfer and staged physical checks. Build completion and
physical acceptance remain pending. Follow [R5 replacement requirements](R5_REPLACEMENT.md).

Historical artifacts and evidence remain unchanged in [r3](r3/README.md) and
[r4](r4/README.md). Preserve them, the old private archives and all journals.
