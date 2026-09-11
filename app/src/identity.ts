/**
 * What to call an agent in the list, decided once.
 *
 * `name` is the address — a DNS label, part of a Fly app name, immutable.
 * `display_name` is what a person calls it and may say anything. The sidebar
 * shows the second when it exists and the first when it does not, and the
 * address never disappears: when a display name is shown, the address takes
 * the small line underneath, because renaming an agent must never look like
 * moving it.
 */

import type { BoxRow } from "./types";

export function labelOf(box: Pick<BoxRow, "id" | "name" | "display_name">): {
  primary: string;
  secondary: string;
} {
  const shown = box.display_name?.trim();
  if (shown) return { primary: shown, secondary: box.name };
  return { primary: box.name, secondary: box.id };
}
