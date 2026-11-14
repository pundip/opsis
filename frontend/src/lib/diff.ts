// A small line diff (longest common subsequence), used to compare dashboard versions.

export interface DiffLine {
  kind: "same" | "added" | "removed";
  text: string;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  // lcs[i][j] = length of the longest common subsequence of a[i..] and b[j..]
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i]![j] = a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      lines.push({ kind: "same", text: a[i]! });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      lines.push({ kind: "removed", text: a[i++]! });
    } else {
      lines.push({ kind: "added", text: b[j++]! });
    }
  }
  while (i < a.length) lines.push({ kind: "removed", text: a[i++]! });
  while (j < b.length) lines.push({ kind: "added", text: b[j++]! });
  return lines;
}

/** Only the changed lines, each with a few unchanged lines around it. */
export function withContext(lines: DiffLine[], context = 2): (DiffLine | "gap")[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.kind === "same") return;
    for (let k = Math.max(0, index - context); k <= Math.min(lines.length - 1, index + context); k++) {
      keep[k] = true;
    }
  });
  const result: (DiffLine | "gap")[] = [];
  lines.forEach((line, index) => {
    if (keep[index]) result.push(line);
    else if (result.at(-1) !== "gap" && result.length > 0) result.push("gap");
  });
  if (result.at(-1) === "gap") result.pop();
  return result;
}

export function countChanges(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((l) => l.kind === "added").length,
    removed: lines.filter((l) => l.kind === "removed").length,
  };
}
