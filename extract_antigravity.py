"""
Extract data from Google Antigravity IDE.

Data locations:
- macOS: ~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb
- Windows: C:\\Users\\USERNAME\\AppData\\Roaming\\Antigravity\\User\\globalStorage\\state.vscdb
- Conversations: ~/.gemini/antigravity/conversations/ (.pb files)
"""

import json
import sqlite3
import sys
from pathlib import Path
import base64
import struct


def find_antigravity_db():
    """Find Antigravity SQLite database based on OS."""
    home = Path.home()
    
    # Check Windows location
    if sys.platform == "win32":
        db_path = home / "AppData" / "Roaming" / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
    # Check macOS location
    elif sys.platform == "darwin":
        db_path = home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
    else:
        db_path = None
    
    if db_path and db_path.exists():
        return db_path
    
    # Try to find it in common locations
    possible_paths = [
        home / "AppData" / "Roaming" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
        home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
    ]
    
    for path in possible_paths:
        if path.exists():
            return path
    
    return None


def extract_trajectory_summaries(db_path):
    """Extract trajectorySummaries from Antigravity database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Query for trajectorySummaries
    cursor.execute(
        "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'"
    )
    
    row = cursor.fetchone()
    if not row:
        print("No trajectorySummaries found in database")
        return []
    
    value = row[0]
    
    # Decode base64
    try:
        decoded = base64.b64decode(value)
        print(f"Decoded {len(decoded)} bytes from trajectorySummaries")
        
        # Save raw decoded data for inspection
        output_path = Path("antigravity_trajectory_raw.bin")
        output_path.write_bytes(decoded)
        print(f"Saved raw data to {output_path}")
        
        # Try to parse as protobuf (simplified - actual protobuf parsing would need protoc)
        # For now, just return the decoded data
        return [{"raw": decoded.hex()}]
    except Exception as e:
        print(f"Error decoding trajectorySummaries: {e}")
        return []
    
    finally:
        conn.close()


def find_conversation_files():
    """Find conversation .pb files."""
    home = Path.home()
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    
    if not conv_dir.exists():
        print(f"Conversation directory not found: {conv_dir}")
        return []
    
    pb_files = list(conv_dir.glob("*.pb"))
    print(f"Found {len(pb_files)} conversation .pb files")
    
    return pb_files


def extract_conversation_metadata(pb_files):
    """Extract metadata from conversation .pb files."""
    conversations = []
    
    for pb_file in pb_files:
        try:
            data = pb_file.read_bytes()
            conversations.append({
                "file": str(pb_file),
                "size": len(data),
                "raw_hex": data[:100].hex()  # First 100 bytes for inspection
            })
        except Exception as e:
            print(f"Error reading {pb_file}: {e}")
    
    return conversations


def main():
    print("Antigravity Data Extraction")
    print("=" * 50)
    
    # Find database
    db_path = find_antigravity_db()
    if not db_path:
        print("Antigravity database not found")
        print("Please ensure Antigravity is installed and has been used")
        return
    
    print(f"Found database: {db_path}")
    
    # Extract trajectory summaries
    trajectory_data = extract_trajectory_summaries(db_path)
    print(f"Extracted {len(trajectory_data)} trajectory entries")
    
    # Find conversation files
    conv_files = find_conversation_files()
    conversations = extract_conversation_metadata(conv_files)
    print(f"Extracted {len(conversations)} conversation files")
    
    # Save to JSON
    output = {
        "trajectory_summaries": trajectory_data,
        "conversations": conversations
    }
    
    output_path = Path("antigravity_extracted.json")
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Saved extraction to {output_path}")


if __name__ == "__main__":
    main()
