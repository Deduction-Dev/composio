import { tool } from "ai";
import { z } from "zod";
import { ComposioToolSet as BaseComposioToolSet } from "../sdk/base.toolset";
import { TELEMETRY_LOGGER } from "../sdk/utils/telemetry";
import { TELEMETRY_EVENTS } from "../sdk/utils/telemetry/events";
import { RawActionData } from "../types/base_toolset";
import { jsonSchemaToModel } from "../utils/shared";
type Optional<T> = T | null;

const zExecuteToolCallParams = z.object({
  actions: z.array(z.string()).optional(),
  apps: z.array(z.string()).optional(),
  params: z.record(z.any()).optional(),
  entityId: z.string().optional(),
  useCase: z.string().optional(),
  usecaseLimit: z.number().optional(),
  connectedAccountId: z.string().optional(),
  tags: z.array(z.string()).optional(),

  filterByAvailableApps: z.boolean().optional().default(false),
});

export class VercelAIToolSet extends BaseComposioToolSet {
  fileName: string = "js/src/frameworks/vercel.ts";
  constructor(
    config: {
      apiKey?: Optional<string>;
      baseUrl?: Optional<string>;
      entityId?: string;
    } = {}
  ) {
    super({
      apiKey: config.apiKey || null,
      baseUrl: config.baseUrl || null,
      runtime: "vercel-ai",
      entityId: config.entityId || "default",
    });
  }

  private generateVercelTool(schema: RawActionData) {
    const parameters = jsonSchemaToModel(schema.parameters);
    return tool({
      description: schema.description,
      parameters,
      execute: async (params: Record<string, string>) => {
        return await this.executeToolCall(
          {
            name: schema.name,
            arguments: JSON.stringify(params),
          },
          this.entityId
        );
      },
    });
  }

  // change this implementation
  async getTools(filters: {
    actions?: Array<string>;
    apps?: Array<string>;
    tags?: Optional<Array<string>>;
    useCase?: Optional<string>;
    usecaseLimit?: Optional<number>;
    filterByAvailableApps?: Optional<boolean>;
  }): Promise<{ [key: string]: RawActionData }> {
    TELEMETRY_LOGGER.manualTelemetry(TELEMETRY_EVENTS.SDK_METHOD_INVOKED, {
      method: "getTools",
      file: this.fileName,
      params: filters,
    });

    const {
      apps,
      tags,
      useCase,
      usecaseLimit,
      filterByAvailableApps,
      actions,
    } = zExecuteToolCallParams.parse(filters);

    const actionsList = await this.client.actions.list({
      ...(apps && { apps: apps?.join(",") }),
      ...(tags && { tags: tags?.join(",") }),
      ...(useCase && { useCase: useCase }),
      ...(actions && { actions: actions?.join(",") }),
      ...(usecaseLimit && { usecaseLimit: usecaseLimit }),
      filterByAvailableApps: filterByAvailableApps ?? undefined,
    });

    const tools = {};
    actionsList.items?.forEach((actionSchema) => {
      // @ts-ignore
      tools[actionSchema.name!] = this.generateVercelTool(
        // @ts-ignore
        actionSchema as ActionData
      );
    });

    return tools;
  }

  async executeToolCall(
    tool: { name: string; arguments: unknown },
    entityId: Optional<string> = null
  ): Promise<string> {
    TELEMETRY_LOGGER.manualTelemetry(TELEMETRY_EVENTS.SDK_METHOD_INVOKED, {
      method: "executeToolCall",
      file: this.fileName,
      params: { tool, entityId },
    });

    return JSON.stringify(
      await this.executeAction({
        action: tool.name,
        params:
          typeof tool.arguments === "string"
            ? JSON.parse(tool.arguments)
            : tool.arguments,
        entityId: entityId || this.entityId,
      })
    );
  }
}
