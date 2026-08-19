#!/bin/bash
# Extract from ALL AI coding assistants at once

echo "================================================================================"
echo "AI CODING ASSISTANT DATA EXTRACTION - ALL TOOLS"
echo "================================================================================"
echo ""

# Create output directory
mkdir -p extracted_data

# Extractors emit temporary per-tool JSONL snapshots. Import them into SQLite,
# keep a tiny rollback window until import succeeds, then delete the snapshots.
KEEP_PER_TOOL=${KEEP_PER_TOOL:-3}

prune_old_outputs() {
    local pattern="$1"
    local files
    files=(extracted_data/${pattern})
    if [ ! -e "${files[0]}" ]; then
        return
    fi
    files=($(ls -1t extracted_data/${pattern} 2>/dev/null))
    for ((i=KEEP_PER_TOOL; i<${#files[@]}; i++)); do
        rm -f "${files[$i]}"
    done
}

rm -f extracted_data/ALL_CONVERSATIONS_*.jsonl

# Track what we found
declare -a found_tools=()
declare -a not_found=()

# Extract from each tool
echo "🔍 Extracting from Claude Code..."
if uv run extract_claude_code.py 2>&1 | tee extracted_data/claude_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Claude Code")
else
    not_found+=("Claude Code")
fi
echo ""

echo "🔍 Extracting from Cursor..."
if uv run extract_cursor.py 2>&1 | tee extracted_data/cursor_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Cursor")
else
    not_found+=("Cursor")
fi
echo ""

echo "🔍 Extracting from Codex..."
if uv run extract_codex.py 2>&1 | tee extracted_data/codex_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Codex")
else
    not_found+=("Codex")
fi
echo ""

echo "🔍 Extracting from Trae..."
if uv run extract_trae.py 2>&1 | tee extracted_data/trae_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Trae")
else
    not_found+=("Trae")
fi
echo ""

echo "🔍 Extracting from Windsurf..."
if uv run extract_windsurf.py 2>&1 | tee extracted_data/windsurf_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Windsurf")
else
    not_found+=("Windsurf")
fi
echo ""

echo "🔍 Extracting from Continue..."
if uv run extract_continue.py 2>&1 | tee extracted_data/continue_extraction.log | grep -q "Found [1-9]"; then
    found_tools+=("Continue")
else
    not_found+=("Continue")
fi
echo ""

echo "🔍 Extracting from Gemini CLI..."
if uv run extract_gemini.py 2>&1 | tee extracted_data/gemini_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Gemini CLI")
else
    not_found+=("Gemini CLI")
fi
echo ""

echo "🔍 Extracting from OpenCode..."
if uv run extract_opencode.py 2>&1 | tee extracted_data/opencode_extraction.log | grep -q "Total conversations extracted: [1-9]"; then
    found_tools+=("OpenCode")
else
    not_found+=("OpenCode")
fi
echo ""

prune_old_outputs "claude_code_conversations_*.jsonl"
prune_old_outputs "cursor_ultimate_*.jsonl"
prune_old_outputs "codex_conversations_*.jsonl"
prune_old_outputs "gemini_conversations_*.jsonl"
prune_old_outputs "opencode_conversations_*.jsonl"
prune_old_outputs "trae_conversations_*.jsonl"
prune_old_outputs "windsurf_conversations_*.jsonl"
prune_old_outputs "continue_conversations_*.jsonl"

uv run conversation_store.py import-jsonl --delete-after-import

echo "================================================================================"
echo "EXTRACTION SUMMARY"
echo "================================================================================"
echo ""

if [ ${#found_tools[@]} -gt 0 ]; then
    echo "✅ Successfully extracted from:"
    for tool in "${found_tools[@]}"; do
        echo "   - $tool"
    done
    echo ""
fi

if [ ${#not_found[@]} -gt 0 ]; then
    echo "⚠️  No data found for:"
    for tool in "${not_found[@]}"; do
        echo "   - $tool"
    done
    echo ""
fi

total_lines=$(sqlite3 extracted_data/conversations.sqlite3 "select count(*) from conversations;" 2>/dev/null || echo 0)

echo "📊 Total conversations stored: $total_lines"
echo ""

echo "📁 Conversation store:"
ls -lh extracted_data/conversations.sqlite3 2>/dev/null | awk '{print "   " $9 " (" $5 ")"}'
echo ""

echo "JSONL snapshots imported into SQLite and removed; downstream why reads the table."
echo ""

echo "================================================================================"
echo "COMPLETE!"
echo "================================================================================"
