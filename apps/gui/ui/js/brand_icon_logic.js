/** Pure provider/backend visual-identity resolution. */
const RULES = [
    [/anthropic|^claude/, "anthropic"],
    [/openai|^gpt[-.\d]|^o[13][-.]|text-embedding/, "openai"],
    [/gemini|gemma/, "gemini"],
    [/^google/, "google"],
    [/cohere|^embed-v|command-r/, "cohere"],
    [/voyage/, "voyage"],
    [/deepseek/, "deepseek"],
    [/^grok|^x-?ai$|^xai/, "xai"],
    [/zhipu|^glm|^zai|bigmodel/, "zai"],
    [/moonshot|^kimi/, "moonshot"],
    [/qwen|z-image/, "qwen"],
    [/dashscope|alibaba/, "alibaba"],
    [/minimax/, "minimax"],
    [/mistral/, "mistral"],
    [/^meta|llama/, "meta"],
    [/huggingface/, "huggingface"],
    [/ollama/, "ollama"],
    [/lmstudio/, "lmstudio"],
    [/vllm/, "vllm"],
    [/openrouter/, "openrouter"],
    [/elasticsearch/, "elasticsearch"],
    [/redis/, "redis"],
    [/neo4j/, "neo4j"],
    [/postgres|pgvector/, "postgres"],
    [/sqlite/, "sqlite"],
    [/mysql/, "mysql"],
    [/duckdb/, "duckdb"],
    [/trino/, "trino"],
    [/milvus/, "milvus"],
    [/surreal/, "surrealdb"],
];
// Infrastructure without a compact official mark uses one consistent
// product glyph rather than a meaningless initial. Names are registered
// backend/provider identifiers, never specific user-created item IDs.
const SYMBOL_RULES = [
    [/^sql$/, "database"],
    [/oceanbase/, "waves"],
    [/mssql|sqlserver|sql-server/, "database"],
    [/oracle/, "database"],
    [/starrocks/, "sparkles"],
    [/clickhouse/, "columns"],
    [/supabase/, "zap"],
    [/seekdb/, "search"],
    [/dolt/, "branch"],
    [/opensearch/, "search"],
    [/mongodb/, "database"],
    [/tinydb/, "file"],
    [/^inmem$/, "server"],
    [/^file$/, "folder"],
    [/^json$/, "braces"],
    [/^pickle$/, "box"],
    [/lance/, "table"],
    [/qdrant/, "box"],
    [/weaviate/, "globe"],
    [/chroma/, "layers"],
    [/pinecone/, "branch"],
];
/** Return the resolved brand or semantic-symbol identity without touching the DOM. */
export function brandIconKind(name) {
    const key = String(name || "").toLowerCase();
    for (const [pattern, brand] of RULES)
        if (pattern.test(key))
            return `brand:${brand}`;
    for (const [pattern, symbol] of SYMBOL_RULES)
        if (pattern.test(key))
            return `symbol:${symbol}`;
    return null;
}
