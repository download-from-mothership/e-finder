# PageIndex Setup Guide
**Note:** The Cowork VM cannot access external git repos. Run these commands on your local machine or VPS.

## Installation

```bash
git clone https://github.com/VectifyAI/PageIndex.git
cd PageIndex
pip3 install --upgrade -r requirements.txt

# Set API key (PageIndex uses GPT-4o by default)
echo "CHATGPT_API_KEY=your_openai_key_here" > .env
```

## Running on E-FINDER Documents

### Recommended: Vision RAG mode for heavily redacted docs
The standard text-based mode will struggle with documents that have:
- Black-box visual redactions (EFTA02846578, EFTA02846673)
- Poor OCR (EFTA02849278)
- Scanned system printouts (EFTA02848586)

See `cookbook/vision_RAG_pageindex.ipynb` for vision-based processing.

### Standard mode for cleaner documents
```bash
# Process a single document
python3 run_pageindex.py \
    --pdf_path /path/to/E-FINDER/EFTA02847772.pdf \
    --max-pages-per-node 10 \
    --if-add-node-summary yes \
    --if-add-doc-description yes

# Recommended starting documents (cleaner OCR):
# 1. EFTA02847772.pdf (51 pages, travel records)
# 2. EFTA02846457.pdf (3 pages, evidence inventory)
# 3. EFTA02847907.pdf (174 pages, booking records)
# 4. EFTA02848582.pdf (4 pages, aircraft report)
```

### Batch processing
```bash
# Process all PDFs
for pdf in /path/to/E-FINDER/EFTA*.pdf; do
    echo "Processing: $pdf"
    python3 run_pageindex.py \
        --pdf_path "$pdf" \
        --max-pages-per-node 10 \
        --if-add-node-summary yes \
        --if-add-doc-description yes
    # Move output to pipeline folder
    mv output/*.json /path/to/E-FINDER/_pipeline_output/pageindex_trees/
done
```

## Output
PageIndex generates a hierarchical JSON tree for each document. Save these to `_pipeline_output/pageindex_trees/` and the `process_and_ingest.py` script will pick them up during MongoDB ingestion.

## Alternative: PageIndex Cloud API or MCP
- Cloud API: https://pageindex.ai
- MCP Server: https://pageindex.ai/mcp (can be added to Claude Desktop)
