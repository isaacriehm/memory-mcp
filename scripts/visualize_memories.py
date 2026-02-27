#!/usr/bin/env python3
"""
Generate an interactive graph of all memories.
Run: python scripts/visualize_memories.py
Requires DATABASE_URL in environment.
Output: memory_map.html (in project root)
"""
import asyncio
import asyncpg
import json
import os
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set.")
    exit(1)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Memory Visualizer</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style>
        body { font-family: 'Inter', sans-serif; margin: 0; padding: 0; background-color: #121212; color: #ffffff; }
        #header { padding: 20px; background-color: #1e1e1e; box-shadow: 0 4px 6px rgba(0,0,0,0.3); z-index: 10; position: relative; display: flex; justify-content: space-between; align-items: center; }
        h1 { margin: 0; font-size: 24px; font-weight: 600; color: #4dabf7; }
        .stats { font-size: 14px; color: #adb5bd; }
        #mynetwork { width: 100vw; height: calc(100vh - 72px); border: none; background-color: #0d0d0d; }
        .vis-network:focus { outline: none; }
        #controls { position: absolute; bottom: 20px; left: 20px; background: rgba(30, 30, 30, 0.9); padding: 15px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); z-index: 10; max-width: 300px; backdrop-filter: blur(10px); }
        .control-group { margin-bottom: 10px; }
        .control-group label { display: block; margin-bottom: 5px; font-size: 12px; color: #adb5bd; font-weight: 600; text-transform: uppercase; }
        select, input { width: 100%; padding: 8px; border-radius: 4px; border: 1px solid #333; background: #222; color: #fff; font-family: inherit; }
        #details-pane { position: absolute; top: 92px; right: 20px; width: 350px; max-height: calc(100vh - 132px); background: rgba(30,30,30,0.95); padding: 20px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); z-index: 10; display: none; overflow-y: auto; backdrop-filter: blur(10px); border: 1px solid #333; }
        #details-pane h3 { margin-top: 0; color: #4dabf7; font-size: 18px; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .detail-item { margin-bottom: 15px; }
        .detail-item .label { font-size: 11px; text-transform: uppercase; color: #868e96; font-weight: 700; margin-bottom: 4px; }
        .detail-item .value { font-size: 14px; color: #e9ecef; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
        .close-btn { position: absolute; top: 15px; right: 15px; background: none; border: none; color: #adb5bd; cursor: pointer; font-size: 16px; transition: color 0.2s; }
        .close-btn:hover { color: #fff; }
    </style>
</head>
<body>
    <div id="header">
        <h1>Omni-Context Memory Graph</h1>
        <div class="stats" id="graph-stats">Loading...</div>
    </div>
    <div id="mynetwork"></div>

    <div id="controls">
        <div class="control-group">
            <label>Physics Engine</label>
            <select id="physics-select">
                <option value="forceAtlas2Based">Force Atlas 2</option>
                <option value="barnesHut">Barnes Hut</option>
                <option value="repulsion">Repulsion</option>
            </select>
        </div>
        <div class="control-group">
            <label>Search Nodes</label>
            <input type="text" id="node-search" placeholder="Type to filter...">
        </div>
    </div>

    <div id="details-pane">
        <button class="close-btn" onclick="document.getElementById('details-pane').style.display='none'">✕</button>
        <h3>Node Details</h3>
        <div class="detail-item">
            <div class="label">ID</div>
            <div class="value" id="detail-id"></div>
        </div>
        <div class="detail-item">
            <div class="label">Category Path</div>
            <div class="value" id="detail-category"></div>
        </div>
        <div class="detail-item">
            <div class="label">Content</div>
            <div class="value" id="detail-content"></div>
        </div>
        <div class="detail-item">
            <div class="label">Metadata & Timestamps</div>
            <div class="value" id="detail-meta"></div>
        </div>
    </div>

    <script type="text/javascript">
        // Inject database data here
        const nodesData = __NODES_JSON__;
        const edgesData = __EDGES_JSON__;

        const nodes = new vis.DataSet(nodesData);
        const edges = new vis.DataSet(edgesData);
        
        const container = document.getElementById('mynetwork');
        
        const datak = {
            nodes: nodes,
            edges: edges
        };
        
        const options = {
            nodes: {
                shape: 'dot',
                size: 20,
                font: {
                    size: 14,
                    color: '#ffffff',
                    face: 'Inter, sans-serif'
                },
                borderWidth: 2,
                shadow: true
            },
            edges: {
                width: 2,
                font: {
                    size: 11,
                    color: '#868e96',
                    align: 'middle',
                    strokeWidth: 0,
                    face: 'Inter, sans-serif'
                },
                color: {
                    color: '#495057',
                    highlight: '#4dabf7',
                    hover: '#ced4da'
                },
                arrows: {
                    to: { enabled: true, scaleFactor: 0.5 }
                },
                smooth: { type: 'continuous' }
            },
            physics: {
                forceAtlas2Based: {
                    gravitationalConstant: -50,
                    centralGravity: 0.01,
                    springLength: 100,
                    springConstant: 0.08,
                    damping: 0.4
                },
                minVelocity: 0.75,
                solver: 'forceAtlas2Based'
            },
            interaction: {
                hover: true,
                tooltipDelay: 200,
                zoomView: true
            },
            groups: {
                // We'll auto-generate colors for groups
            }
        };

        // Create groups dynamically
        const categories = [...new Set(nodesData.map(n => n.group))];
        const colors = ['#f03e3e', '#d6336c', '#ae3ec9', '#7048e8', '#4263eb', '#1c7ed6', '#1098ad', '#0ca678', '#37b24d', '#74b816', '#f59f00', '#f76707'];
        
        categories.forEach((cat, index) => {
            const colorIndex = index % colors.length;
            options.groups[cat] = {
                color: {
                    background: colors[colorIndex],
                    border: '#ffffff',
                    highlight: { background: '#ffffff', border: colors[colorIndex] },
                    hover: { background: colors[colorIndex], border: '#ffffff' }
                }
            };
        });

        const network = new vis.Network(container, datak, options);
        
        // Stats
        document.getElementById('graph-stats').textContent = 
            `${nodesData.length} Memories | ${edgesData.length} Connections`;

        // Interaction events
        network.on("selectNode", function (params) {
            const nodeId = params.nodes[0];
            const nodeInfo = nodes.get(nodeId);
            
            document.getElementById('detail-id').textContent = nodeInfo.id;
            document.getElementById('detail-category').textContent = nodeInfo.group;
            document.getElementById('detail-content').textContent = nodeInfo.fullContent;
            
            const metaStr = `Created: ${nodeInfo.created_at}\\nUpdated: ${nodeInfo.updated_at}\\nAccessed: ${nodeInfo.last_accessed_at}\\n\\nMetadata: ${nodeInfo.metadata}`;
            document.getElementById('detail-meta').textContent = metaStr;
            
            document.getElementById('details-pane').style.display = 'block';
        });

        network.on("deselectNode", function (params) {
            document.getElementById('details-pane').style.display = 'none';
        });

        // Controls
        document.getElementById('physics-select').addEventListener('change', (e) => {
            network.setOptions({ physics: { solver: e.target.value } });
        });

        const searchInput = document.getElementById('node-search');
        searchInput.addEventListener('input', (e) => {
            const term = e.target.value.toLowerCase();
            if(!term) {
                nodes.forEach(n => nodes.update({id: n.id, hidden: false}));
                return;
            }
            
            const toUpdate = [];
            nodes.forEach(n => {
                const match = n.fullContent.toLowerCase().includes(term) || n.group.toLowerCase().includes(term);
                toUpdate.push({id: n.id, hidden: !match});
            });
            nodes.update(toUpdate);
        });

    </script>
</body>
</html>
"""

async def export_capabilities():
    print(f"Connecting to database at {DATABASE_URL} ...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"FAILED to connect to DB: {e}")
        return

    try:
        print("Fetching memories...")
        memories_records = await conn.fetch("SELECT id, content, category_path::text, metadata::text, created_at, updated_at, last_accessed_at FROM memories WHERE supersedes_id IS NULL")

        print("Fetching edges...")
        edges_records = await conn.fetch("""
            SELECT e.source_id, e.target_id, e.relation_type
            FROM memory_edges e
            JOIN memories src ON src.id = e.source_id
            JOIN memories tgt ON tgt.id = e.target_id
            WHERE src.supersedes_id IS NULL AND tgt.supersedes_id IS NULL
        """)
    finally:
        await conn.close()

    nodes = []
    for r in memories_records:
        content = r['content']
        label = content[:40] + "..." if len(content) > 40 else content
        nodes.append({
            "id": str(r['id']),
            "label": label,
            "title": label,
            "group": r['category_path'],
            "fullContent": content,
            "metadata": r['metadata'],
            "created_at": str(r['created_at']),
            "updated_at": str(r['updated_at']),
            "last_accessed_at": str(r['last_accessed_at'])
        })

    edges = []
    for r in edges_records:
        edges.append({
            "from": str(r['source_id']),
            "to": str(r['target_id']),
            "label": r['relation_type'],
            "title": r['relation_type']
        })

    print(f"Extracted {len(nodes)} nodes and {len(edges)} edges.")

    html_output = HTML_TEMPLATE.replace("__NODES_JSON__", json.dumps(nodes)).replace("__EDGES_JSON__", json.dumps(edges))

    # Write to project root (parent of scripts/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    out_file = os.path.join(project_root, "memory_map.html")
    with open(out_file, "w") as f:
        f.write(html_output)

    print(f"Successfully wrote interactive visualization to {out_file}")

if __name__ == "__main__":
    asyncio.run(export_capabilities())
