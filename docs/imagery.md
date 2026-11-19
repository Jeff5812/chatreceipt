# Imagery

How the pictures on the site are made, and where the earlier photographs went.

## The rule

Since 18 November 2026 every picture on the site is **pixel art drawn in code**, in the same language as the
rest of the interface:

1. **Chit**, the mascot: a receipt slip with eyes (`frontend/components/chit.tsx`, made interactive in
   `frontend/components/mascot.tsx`).
2. **Sprites with depth**: small objects (phone, coin, chat bubble, magnifier, tick) in
   `frontend/components/pixel-art.tsx`, extruded so they read as blocks rather than flat stickers.
3. **Pixel icons**: `frontend/components/ui/icons.tsx` and the payment status shapes in `frontend/components/ui/status.tsx`.

Pixel art never stands in for the real product. The record, the figures and the chat text are always real,
live text. No recognisable faces, real logos or anything that reads as a testimonial.

## How a sprite works

A sprite is a grid of characters and a palette:

```ts
export const TICK = {
  grid: ["LLLLLLLLL", "LLLLLLLKL", "..."],
  palette: { L: MATERIALS.lime, K: MATERIALS.ink },
};
```

- `.` is empty. Every other character maps to a **material**: a face colour and a darker side colour.
- `<PixelArt {...TICK} cell={5} depth={2} />` draws it with 5px cells and extrudes it two cells down and to the
  right in the side colours. That extrusion is what makes it 3D.
- Rows are merged into runs, so a sprite is a few dozen rects. Rendering is `crispEdges`: keep `cell` a whole
  number of pixels.

The materials (`MATERIALS` in `pixel-art.tsx`) keep every sprite in the palette: paper, line, lime, ink,
silver, frame and screen. Lime is the one bright colour; use it for the thing that matters in the sprite.

**To add a sprite:** draw it on a grid of 16 cells or fewer (bigger loses the pixel feel), use existing
materials, then look at it rendered at its real size and at 3x. Check that any symbol reads correctly: the
first coin drew a ₦ that read as #.

## Chit's moods

`chit.tsx` holds the fixed body (12 x 13 cells) and draws only the eyes per state:

| Mood | Eyes | Used for |
|---|---|---|
| `idle` | Two tall blocks that look towards the pointer and blink every few seconds | Everywhere by default |
| `happy` | Closed, smiling arches | Hovering the logo, pressing Chit in the hero, files dragged over the upload tray |
| `reading` | Short blocks running left and right | While screenshots are read |
| `worried` | Low, short blocks | Spare, for error states |

`ChitTile` is the still mark on a dark tile, for white paper, the favicon (`frontend/app/icon.svg`) and the
PDF header (`backend/app/documents/templates/record.html`). The PDF copy is generated from the same grid; if
the art changes, regenerate it.

Chit reacts to the app through `cheer(mood, ms)` in `frontend/lib/chit.ts`: the header Chit smiles when a
record is ready or a download finishes, and worries when reading or exporting fails.

## Chit's friends

`frontend/components/critters.tsx` adds two characters who appear only when a record is ready: **Kobo**, a
silver coin named after the naira's smaller unit, and a **lime chat bubble**. They use the same eye states as
Chit (blinking, happy arches) and hop in turn with Chit under a short burst of pixel confetti. Keep the cast
small: more characters would crowd the moment.

## The floating environment

Depth comes from light: sections are lit faintly from above (`lit`), the hero and closing section stand on a
perspective pixel floor (`.floor` in `frontend/app/globals.css`), and floating objects cast a soft shadow on
it. The floor's tilted plane is clipped to its box; without that, its near edge projects far below the page
and adds empty scroll.

## The archived photographs and 3D renders

From 11 to 17 November 2026 the landing page used AI-generated photographs (hands holding a phone, an order
being packed) and 3D clay objects. They are kept, unused, in
`archive/2026-11-18-editorial-design/assets/imagery/`, with the components that showed them.

All of them were generated with FLUX.1 schnell (Apache 2.0) through AI Horde, a free volunteer-run service, used
anonymously. The prompts are in `tools/imagery/prompts/`, and `tools/imagery/` still has the scripts
(`generate.py`, `horde_batch.py`, `prepare.py` for cropping and grading, `cutout_on_black.py` for objects
rendered on black).

| File | Prompt | Seed | Generated |
|---|---|---|---|
| `hero-hands.jpg` | `hero.txt` | 21 | 16 Nov 2026 |
| `step-chat.jpg`, `step-review.jpg` | `step-chat.txt`, `step-review.txt` | 3 | 16 Nov 2026 |
| `step-record.jpg` | `step-record.txt` | 14 | 16 Nov 2026 |
| `use-seller.jpg`, `use-freelancer.jpg`, `use-buyer.jpg` | `use-*.txt` | 5 | 11 Nov 2026 |
| `cta-linen.jpg` | `cta-linen.txt` | 8 | 11 Nov 2026 |

What was tried for image generation, for the record: Gemini image models through the API (free-tier quota is
zero), the FLUX.1 schnell demo on Hugging Face (excellent, but two images a day without a token), AI Horde
(works, about one megapixel, 5 to 12 minutes per batch), Pollinations (low quality, watermarked). Claude
Design canvases are HTML and only place images you upload; they don't generate them.
