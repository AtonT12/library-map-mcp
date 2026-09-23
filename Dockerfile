FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build-time asset assertion (plan Step 1-b): fail the image build if a
# required asset was not packaged, instead of failing at first request.
RUN python src/selfcheck.py
# Pre-generate the downsampled inline base maps for the MCP App view.
RUN python scripts/build_static.py

EXPOSE 8000

CMD ["python", "src/mcp_server.py"]
