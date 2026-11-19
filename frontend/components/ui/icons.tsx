/**
 * Pixel icons. Each is drawn on an 8 x 8 grid ('#' is a filled cell) and rendered with crisp edges, so at
 * the usual 16px every cell is exactly 2 screen pixels. They take the current text colour.
 */

interface IconProps {
  className?: string;
}

/** Turn a grid into one path of unit squares, merging runs along each row. */
function toPath(grid: readonly string[]): string {
  let d = "";
  grid.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      if (row[x] !== "#") {
        x += 1;
        continue;
      }
      let w = 1;
      while (row[x + w] === "#") w += 1;
      d += `M${x} ${y}h${w}v1h-${w}z`;
      x += w;
    }
  });
  return d;
}

function pixelIcon(grid: readonly string[], fallback: string) {
  const d = toPath(grid);
  const size = grid.length;
  function Icon({ className = fallback }: IconProps) {
    return (
      <svg viewBox={`0 0 ${size} ${size}`} className={className} fill="currentColor" shapeRendering="crispEdges" aria-hidden>
        <path d={d} />
      </svg>
    );
  }
  return Icon;
}

export const CloseIcon = pixelIcon(["#......#", "##....##", ".##..##.", "..####..", "..####..", ".##..##.", "##....##", "#......#"], "size-4");

export const ChatIcon = pixelIcon([".######.", "########", "#.#..#.#", "########", "########", ".######.", "..##....", "..#....."], "size-4");

export const ArrowRightIcon = pixelIcon(["....#...", "....##..", "....###.", "########", "########", "....###.", "....##..", "....#..."], "size-4");

export const ArrowLeftIcon = pixelIcon(["...#....", "..##....", ".###....", "########", "########", ".###....", "..##....", "...#...."], "size-4");

export const PlusIcon = pixelIcon(["...##...", "...##...", "...##...", "########", "########", "...##...", "...##...", "...##..."], "size-4");

export const CheckIcon = pixelIcon(["........", ".......#", "......##", "#....##.", "##..##..", ".####...", "..##....", "........"], "size-4");

export const InfoIcon = pixelIcon([".######.", "#......#", "#..##..#", "#......#", "#..##..#", "#..##..#", "#......#", ".######."], "size-4");

export const AlertIcon = pixelIcon(["...##...", "..#..#..", "..#..#..", ".#.##.#.", ".#.##.#.", "#......#", "#..##..#", "########"], "size-4");

export const ErrorIcon = pixelIcon([".######.", "##....##", "#.#..#.#", "#..##..#", "#..##..#", "#.#..#.#", "##....##", ".######."], "size-4");

export const DownloadIcon = pixelIcon(["...##...", "...##...", "...##...", ".######.", "..####..", "...##...", "#......#", "########"], "size-4");
