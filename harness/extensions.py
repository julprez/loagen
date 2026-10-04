"""Adaptadores externos activados por configuración del usuario."""
import re

from .mcp import StdioClient
from .tools import Tool


def mcp_tools(servers: list[dict]) -> dict[str, Tool]:
    tools = {}
    for server in servers:
        name = server.get('name', '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ValueError('nombre de servidor MCP inválido')
        command = server.get('command')
        if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
            raise ValueError('command MCP debe ser una lista no vacía')
        # Explicit schemas avoid running a server just to advertise its catalogue.
        for item in server.get('tools', []):
            remote = item.get('name', '')
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', remote):
                raise ValueError('nombre de herramienta MCP inválido')
            public = f'mcp_{name}_{remote}'
            if public in tools:
                raise ValueError('herramienta MCP duplicada: ' + public)
            schema = item.get('inputSchema')
            if not isinstance(schema, dict) or schema.get('type') != 'object':
                raise ValueError('inputSchema MCP debe ser un objeto')

            def handler(args, ctx, server=server, remote=remote, schema=schema):
                timeout = min(float(server.get('timeout', 10)), 30)
                if ctx.budget:
                    timeout = min(timeout, max(.01, ctx.budget.remaining()))
                def checked_call(client):
                    available = {t['name']: t for t in client.list_tools()}
                    if remote not in available or available[remote].get('inputSchema') != schema:
                        raise ValueError('el esquema MCP cambió: revisa y autoriza la configuración')
                    client.timeout = timeout
                    return client.call_tool(remote, args)
                if ctx.resources is not None:
                    key = tuple(server['command'])
                    if key not in ctx.clients:
                        ctx.clients[key] = ctx.resources.enter_context(StdioClient(server['command'], ctx.workspace, timeout))
                    return checked_call(ctx.clients[key])
                with StdioClient(server['command'], ctx.workspace, timeout) as client:
                    return checked_call(client)

            tools[public] = Tool(public, f'MCP {name}: {remote} (ejecuta servidor externo autorizado).',
                                 schema, handler, mutating=True)
    if len(tools) > 20:
        raise ValueError('máximo 20 herramientas MCP configuradas')
    return tools
