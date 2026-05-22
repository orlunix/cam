# CAM Desktop Docs

This directory holds the active Desktop planning and review documents.

## Files

- `requirements.md` — canonical requirement registry with stable Req IDs.
- `../desktop-ui-spec.md` — current milestone/product spec. It explains design
  direction, but implementation and review should cite `requirements.md` IDs.
- `../windows-installer.md` — Windows packaging/install notes.
- `../archive/` — old specs and reference evaluations that should not be used
  as current requirements unless promoted into `requirements.md`.

## Workflow

1. Add or update requirements in `requirements.md`.
2. Assign implementation by Req ID.
3. Review implementation by Req ID.
4. Move outdated milestone specs to `docs/archive/` only when the active
   requirement registry no longer references them.
