// node --experimental-strip-types speakers.check.ts: speakerColor, initial, speakerList (src/lib/speakers.ts), both languages
import assert from "node:assert/strict";
import { i18n } from "./src/i18n.ts";
import {
  initial,
  labelText,
  likeness,
  speakerColor,
  speakerLabel,
  speakerList,
} from "./src/lib/speakers.ts";

assert.equal(speakerColor("我"), "var(--accent)");
for (const s of ["S2", "S?", "", null, undefined]) assert.equal(speakerColor(s), "var(--muted)");
assert.equal(speakerColor("同事A"), speakerColor("同事A"));
assert.match(speakerColor("同事A"), /^#[0-9a-f]{6}$/);
assert.equal(
  new Set(["妈妈", "同事A", "同事B", "室友", "小王", "Alice"].map(speakerColor)).size > 1,
  true,
);
assert.deepEqual(["同事A", "alice", "S2", "S?", "𠮷野", null].map(initial), [
  "同",
  "A",
  "2",
  "?",
  "𠮷",
  "?",
]);
assert.deepEqual(["S2", "S12", "S?", "我", "同事A", null].map(speakerLabel), [
  "说话人 2",
  "说话人 12",
  "未识别",
  "我",
  "同事A",
  "—",
]);
assert.deepEqual([0.72, 0.7, 0.69].map(likeness), ["很像", "很像", "有点像"]);
assert.deepEqual(
  [[], ["S1"], ["S1", "我"], ["S1", "S2", "我"], ["S1", "S2", "S3"]].map(speakerList),
  ["", "说话人 1", "我、说话人 1", "我、说话人 1 等 3 人", "说话人 1、说话人 2 等 3 人"],
);
assert.equal(
  labelText("我和 S1 聊了猫，S12说S?没听清；S3 存储和 Galaxy S24 不是人，S1x 也不是", [
    "我",
    "S1",
    "S12",
  ]),
  "我和 说话人 1 聊了猫，说话人 12说未识别没听清；S3 存储和 Galaxy S24 不是人，S1x 也不是",
);
assert.equal(
  labelText("S2 提醒水开了"),
  "说话人 2 提醒水开了",
  "no labels given (a day's recap): every one",
);
i18n.global.locale.value = "en"; // 我 stays the key in the data, the page says Me
assert.deepEqual(["S2", "S?", "我", "同事A"].map(speakerLabel), [
  "Speaker 2",
  "Unidentified",
  "Me",
  "同事A",
]);
assert.deepEqual(["我", "alice"].map(initial), ["M", "A"]);
assert.equal(speakerList(["S1", "S2", "我"]), "Me, Speaker 1 and 1 more");
assert.equal(
  labelText("S1 and I agreed; Amazon S3 stays", ["S1"]),
  "Speaker 1 and I agreed; Amazon S3 stays",
);
assert.equal(speakerColor("我"), "var(--accent)");
console.log("speakers ok");
