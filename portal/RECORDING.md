# Recording a new portal flow

Each tool in this server wraps a scripted browser flow. To capture the
real click path and selectors for a flow:

1. Launch codegen against the portal with the shared profile (stays
   logged in):

   ```bash
   npx playwright codegen --user-data-dir ~/.amd-playwright-profile https://login.advancedmd.com
   ```

2. Perform the flow by hand once (e.g. open a patient, click the
   Insurance tab). Codegen writes the equivalent Python calls in its
   inspector window.

3. Paste the generated steps into the flow module (e.g.
   `src/amd_portal_mcp/flows/insurance.py`) between the BEGIN/END
   recorded-navigation markers, and update the field selectors in
   `grab()` to match the labels on the final screen.

4. Sanity-run the tool directly:

   ```bash
   cd amd-portal-mcp
   uv run python -c "
   import asyncio, json
   from amd_portal_mcp import browser
   from amd_portal_mcp.flows import insurance
   async def go():
       page = await browser.get_page()
       print(json.dumps(await insurance.get_insurance_details(page, '<last, first or chart#>'), indent=2))
       await browser.shutdown()
   asyncio.run(go())"
   ```

   Aaron runs step 4 with a real patient and eyeballs the output;
   agents should verify with synthetic/test patients only.

The recorded insurance navigation chain (login iframe -> popup app
window -> scheduler iframe -> patient card iframe -> nested legacy
insurance iframe) is documented in `docs/insurance-flow.md`; read it
before recording a flow that starts from the same places. Only one
process can hold the shared profile: stop any stray chromium/codegen
processes before launching.

Adding a new tool = new module under `flows/` + one `@mcp.tool()`
wrapper in `server.py` that routes through `flows/_runner.py`
`run_flow()` (timeout, re-login retry, structured errors) and returns a
bounded, whitelisted dict as `{"ok": true, "data": {...}}`.

If a flow fails, check `runtime/debug/` for the failure screenshot and
page URL (local only, gitignored).
