// OpenCode 2.1.4 native plugin. HTTP/TLS remains owned by OpenCode.
export default {
  id: "tsz.agent.session-budget-v1",
  async setup(ctx) {
    const owned = async (sessionID) => {
      const session = await ctx.session.get({ sessionID });
      const budget = session.metadata?.tszAgent;
      if (!budget) return;
      if (budget.protocol !== "session-generate-budget-v3" ||
          !Number.isInteger(budget.maxOutputTokens) ||
          budget.maxOutputTokens < 1 || budget.maxOutputTokens > 3000) {
        throw new Error("tsz_invalid_output_budget");
      }
      return { session, budget };
    };
    await ctx.session.hook("generate", async (event) => {
      const state = await owned(event.sessionID);
      if (state) event.options.maxTokens = state.budget.maxOutputTokens;
    });
    await ctx.session.hook("http.request", async (event) => {
      const state = await owned(event.sessionID);
      if (!state) return;
      if (event.kind !== "generate" || state.budget.dispatched) {
        throw new Error("tsz_extra_model_request_forbidden");
      }
      // Provider body overlays can override generation options. Bound the final wire body too.
      const body = await event.request.clone().json();
      const fields = ["max_tokens", "max_completion_tokens", "max_output_tokens"]
        .filter((key) => Object.hasOwn(body, key));
      if (body.generationConfig && Object.hasOwn(body.generationConfig, "maxOutputTokens")) {
        fields.push("generationConfig");
      }
      if (fields.length !== 1) throw new Error("tsz_output_budget_not_applied");
      if (fields[0] === "generationConfig") {
        body.generationConfig.maxOutputTokens = state.budget.maxOutputTokens;
      } else {
        body[fields[0]] = state.budget.maxOutputTokens;
      }
      await ctx.session.update({
        sessionID: event.sessionID,
        metadata: { ...state.session.metadata, tszAgent: { ...state.budget, dispatched: true } },
      });
      const headers = new Headers(event.request.headers);
      headers.delete("content-length");
      event.request = new Request(event.request, { headers, body: JSON.stringify(body) });
    });
    await ctx.tool.hook("execute.before", async (event) => {
      if (await owned(event.sessionID)) throw new Error("tsz_tool_execution_forbidden");
    });
  },
};
