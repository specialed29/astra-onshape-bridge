# Astra Onshape Bridge

Hosted MCP bridge between GPT-6 Astra and Onshape.

## Required secrets

- OPENAI_API_KEY
- ONSHAPE_ACCESS_KEY
- ONSHAPE_SECRET_KEY

## Deploy

This repo includes `render.yaml`. Create a Render Blueprint from this repository, enter the three secret values when prompted, then deploy.

Health check: `/health`
MCP endpoint: `/mcp`
Browser UI: `/`

## First test

1. `List my Onshape documents. Do not modify anything.`
2. `Create a document named Astra MCP Test.`
3. `Export a selected Part Studio to STEP.`

The service intentionally exposes only a small initial CAD vocabulary. Expand typed tools before relying on raw feature JSON for production work.
