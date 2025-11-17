#!/bin/bash
# Extract from ALL AI coding assistants at once

echo "================================================================================"
echo "AI CODING ASSISTANT DATA EXTRACTION - ALL TOOLS"
echo "================================================================================"
echo ""

# Create output directory
mkdir -p extracted_data

# Track what we found
declare -a found_tools=()
declare -a not_found=()

# Extract from each tool
echo "🔍 Extracting from Claude Code..."
if python3 extract_claude_code.py 2>&1 | tee extracted_data/claude_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Claude Code")
else
    not_found+=("Claude Code")
fi
echo ""

echo "🔍 Extracting from Cursor..."
if python3 extract_cursor.py 2>&1 | tee extracted_data/cursor_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Cursor")
else
    not_found+=("Cursor")
fi
echo ""

echo "🔍 Extracting from Codex..."
if python3 extract_codex.py 2>&1 | tee extracted_data/codex_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Codex")
else
    not_found+=("Codex")
fi
echo ""

echo "🔍 Extracting from Trae..."
if python3 extract_trae.py 2>&1 | tee extracted_data/trae_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Trae")
else
    not_found+=("Trae")
fi
echo ""

echo "🔍 Extracting from Windsurf..."
if python3 extract_windsurf.py 2>&1 | tee extracted_data/windsurf_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Windsurf")
else
    not_found+=("Windsurf")
fi
echo ""

echo "🔍 Extracting from Continue..."
if python3 extract_continue.py 2>&1 | tee extracted_data/continue_extraction.log | grep -q "Found [1-9]"; then
    found_tools+=("Continue")
else
    not_found+=("Continue")
fi
echo ""

echo "🔍 Extracting from Gemini CLI..."
if python3 extract_gemini.py 2>&1 | tee extracted_data/gemini_extraction.log | grep -q "Total conversations: [1-9]"; then
    found_tools+=("Gemini CLI")
else
    not_found+=("Gemini CLI")
fi
echo ""

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

# Count total conversations
total_lines=$(cat extracted_data/*.jsonl 2>/dev/null | wc -l | tr -d ' ')

echo "📊 Total conversations extracted: $total_lines"
echo ""

# Show file sizes
echo "📁 Output files:"
ls -lh extracted_data/*.jsonl 2>/dev/null | awk '{print "   " $9 " (" $5 ")"}'
echo ""

# Create combined file
combined_file="extracted_data/ALL_CONVERSATIONS_$(date +%Y%m%d_%H%M%S).jsonl"
cat extracted_data/*.jsonl 2>/dev/null | grep -v "extraction.log" > "$combined_file"

if [ -f "$combined_file" ]; then
    combined_size=$(ls -lh "$combined_file" | awk '{print $5}')
    echo "✅ Combined file created:"
    echo "   $combined_file ($combined_size)"
    echo ""
fi

echo "================================================================================"
echo "COMPLETE!"
echo "================================================================================"
