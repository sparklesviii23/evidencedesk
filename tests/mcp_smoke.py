"""Spawn the MCP server over stdio and drive it as an agent would (lean toolset). Prints response sizes.

Usage: EVIDENCEDESK_HOME=<workspace with a review> python tests/mcp_smoke.py [review_id]
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RID = int(sys.argv[1]) if len(sys.argv) > 1 else 1


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "evidencedesk", "mcp"], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            names = [t.name for t in tools]
            schema_chars = sum(len(json.dumps(t.input_schema)) + len(t.description or "") for t in tools)
            assert "set_credential" not in names
            print(f"{len(tools)} tools, {schema_chars} chars of schemas+descriptions: {', '.join(names)}")

            async def call(tool, **kw):
                res = await s.call_tool(tool, kw)
                txt = res.content[0].text
                print(f"\n>>> {tool}({json.dumps(kw)[:80]}) -> {len(txt)} chars")
                print(txt[:700] + ("…" if len(txt) > 700 else ""))
                return txt
            await call("guide")
            await call("status")
            await call("status", review_id=RID)
            await call("plan_search", review_id=RID, query="TITLE_ABSTRACT(ANGPTL3 AND (CRISPR OR \"base editing\"))")
            await call("batch", review_id=RID, stage="ta", n=3, abstract_chars=300)
            await call("read", report_id="RPT-3", find="mice")
            await call("op", name="prisma_counts", args={"review_id": RID})
            await call("ops", filter="study")
            await call("plan_search", review_id=RID, query="TITLE(gene editing)")

asyncio.run(main())
