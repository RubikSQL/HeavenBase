import { describe, expect, test } from "bun:test";

import {
  assistantPresetSelection,
  assistantPresetSummaries,
  browserPanelBounds,
  defaultEnvVariable,
  parseEnvReference,
  boundedHistoryPayload,
  HISTORY_TURN_CEILING,
  parseTypedScalar,
  renameConfigKey,
  replayableHistory,
  statsSeparatorValues,
} from "../ui/src/gui-logic.ts";
import { brandIconKind } from "../ui/src/brand_icon_logic.ts";

describe("typed GUI transitions", () => {
  test("config key renames preserve values and reject collisions", () => {
    expect(renameConfigKey({ first: 1, second: 2 }, "first", "renamed")).toEqual({ renamed: 1, second: 2 });
    expect(() => renameConfigKey({ first: 1, second: 2 }, "first", "second")).toThrow("already exists");
  });

  test("environment-backed fields parse fallbacks without knowing provider items", () => {
    // biome-ignore lint/suspicious/noTemplateCurlyInString: the parser consumes this literal config syntax.
    expect(parseEnvReference('${env:OPENROUTER_API_KEY,""}')).toEqual({
      name: "OPENROUTER_API_KEY",
      fallback: "",
    });
    // biome-ignore lint/suspicious/noTemplateCurlyInString: the parser consumes this literal config syntax.
    expect(parseEnvReference("${env:OPENROUTER_BASE_URL,https://openrouter.ai/api/v1}")).toEqual({
      name: "OPENROUTER_BASE_URL",
      fallback: "https://openrouter.ai/api/v1",
    });
    expect(parseEnvReference("literal")).toBeNull();
    expect(defaultEnvVariable("moonshot-zh", "base_url")).toBe("MOONSHOT_ZH_BASE_URL");
  });

  test("assistant preset summaries resolve model aliases and reasoning inheritance", () => {
    const summaries = assistantPresetSummaries({
      heavenbase: {
        llm: {
          default_args: { think: false, reasoning_effort: "low" },
          models: {
            primary: { aliases: ["p"], default_args: { think: true, reasoning_effort: "high" } },
          },
          presets: {
            inherited: { model: "p" },
            override: { model: "p", default_args: { think: false, reasoning_effort: "max" } },
          },
        },
      },
    });
    expect(summaries).toEqual([
      { value: "inherited", label: "inherited", model: "p", think: true, reasoningEffort: "high" },
      { value: "override", label: "override", model: "p", think: false, reasoningEffort: "max" },
    ]);
  });

  test("assistant preset selection prefers saved state, then the configured chat preset", () => {
    const tree = {
      heavenbase: {
        dashboard: { assistant: { preset: "chat" } },
        llm: {
          default_preset: "system",
          presets: { system: { model: "tiny" }, chat: { model: "chat-model" }, reason: { model: "reasoner" } },
        },
      },
    };
    expect(assistantPresetSelection(tree)).toBe("chat");
    expect(assistantPresetSelection(tree, "reason")).toBe("reason");
    expect(assistantPresetSelection(tree, "removed")).toBe("chat");
  });

  test("stored history matches the server's 40-turn replay window", () => {
    expect(HISTORY_TURN_CEILING).toBe(40);
    const small = Array.from({ length: 250 }, (_, index) => ({ role: "user", content: `t${index}` }));
    const kept = JSON.parse(boundedHistoryPayload(small));
    expect(kept.length).toBe(40);
    expect(kept[0].content).toBe("t210"); // the newest turns survive
    const bulky = Array.from({ length: 40 }, (_, index) => ({
      role: "user",
      content: `${index}:${"x".repeat(100_000)}`,
    }));
    const payload = boundedHistoryPayload(bulky);
    expect(payload.length).toBeLessThanOrEqual(2_000_000); // the 2MB guard is the real backstop
    expect(JSON.parse(payload).length % 2).toBe(0); // whole pairs dropped from the front
  });

  test("replayable history drops failed turns together with their questions", () => {
    expect(
      replayableHistory([
        { role: "user", content: "q1" },
        { role: "assistant", content: "", failure: "stream went silent" },
        { role: "user", content: "q2" },
        { role: "assistant", content: "transport exploded", error: true },
        { role: "user", content: "q3" },
        { role: "assistant", content: "fine" },
        { role: "user", content: "q4" },
        { role: "assistant", content: "partial answer", failure: "stream went silent" },
      ]),
    ).toEqual([
      // Empty-failure and error turns vanish with q1/q2; a failed turn that
      // streamed real content keeps its answer, minus the panel-only markers.
      { role: "user", content: "q3" },
      { role: "assistant", content: "fine" },
      { role: "user", content: "q4" },
      { role: "assistant", content: "partial answer" },
    ]);
  });

  test("every registered backend family and built-in SQL provider has a real icon", () => {
    const backendTypes = [
      "sql",
      "sqlite",
      "duckdb",
      "postgres",
      "supabase",
      "trino",
      "clickhouse",
      "mysql",
      "seekdb",
      "dolt",
      "starrocks",
      "oceanbase",
      "mssql",
      "oracle",
      "pgvector",
      "lance",
      "milvus",
      "milvus-lite",
      "chroma",
      "pinecone",
      "qdrant",
      "weaviate",
      "redisearch",
      "elasticsearch",
      "opensearch",
      "mongodb",
      "tinydb",
      "neo4j",
      "surreal",
      "redis",
      "inmem",
      "file",
      "json",
      "pickle",
    ];
    expect(backendTypes.filter((name) => !brandIconKind(name))).toEqual([]);
  });

  test("typed scalar parsing never accepts numeric prefixes", () => {
    expect(parseTypedScalar("12", "integer")).toBe(12);
    expect(parseTypedScalar("12x", "integer")).toBe("12x");
    expect(parseTypedScalar("false", "boolean")).toBe(false);
  });

  test("responsive separator ranges stay bounded", () => {
    expect(browserPanelBounds(true, 100)).toEqual({
      min: 120,
      max: 120,
      axis: "--browser-h",
      orientation: "horizontal",
    });
    expect(
      statsSeparatorValues({
        stacked: true,
        viewportHeight: 796,
        columnHeight: 302,
        treeHeight: 117,
        panelHeight: 40,
      }),
    ).toEqual({
      tree: { min: 24, max: 452, now: 117 },
      panel: { min: 40, max: 375, now: 40 },
    });
  });
});
