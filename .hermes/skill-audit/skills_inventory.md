# Hermes Skills Inventory

- Generated: 2026-05-28T23:35:16+02:00
- Total SKILL.md files inventoried: 308
- Scope: `~/.hermes/skills` plus Hermes repo skill folders; vendored dependency folders excluded.

## Summary

- High priority: 101
- Medium priority: 47
- Low priority: 160

## Inventory

### 1. blackbox

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/autonomous-ai-agents/blackbox/SKILL.md`
- Name: blackbox
- Zweck: Delegate coding tasks to Blackbox AI CLI agent. Multi-model agent with built-in judge that runs tasks through multiple LLMs and picks the best result. Requires the blackbox CLI and a Blackbox AI API key.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 2. honcho

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/autonomous-ai-agents/honcho/SKILL.md`
- Name: honcho
- Zweck: Configure and use Honcho memory with Hermes -- cross-session user modeling, multi-profile peer isolation, observation config, dialectic reasoning, session summaries, and context budget enforcement. Use when setting up Honcho, troubleshootin
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 3. openhands

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/autonomous-ai-agents/openhands/SKILL.md`
- Name: openhands
- Zweck: Delegate coding to OpenHands CLI (model-agnostic, LiteLLM).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 4. evm

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/blockchain/evm/SKILL.md`
- Name: evm
- Zweck: Read-only EVM client: wallets, tokens, gas across 8 chains.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 5. hyperliquid

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/blockchain/hyperliquid/SKILL.md`
- Name: hyperliquid
- Zweck: Hyperliquid market data, account history, trade review.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 6. solana

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/blockchain/solana/SKILL.md`
- Name: solana
- Zweck: Query Solana blockchain data with USD pricing — wallet balances, token portfolios with values, transaction details, NFTs, whale detection, and live network stats. Uses Solana RPC + CoinGecko. No API key required.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 7. one-three-one-rule

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/communication/one-three-one-rule/SKILL.md`
- Name: one-three-one-rule
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 8. blender-mcp

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/creative/blender-mcp/SKILL.md`
- Name: blender-mcp
- Zweck: Control Blender directly from Hermes via socket connection to the blender-mcp addon. Create 3D objects, materials, animations, and run arbitrary Blender Python (bpy) code. Use when user wants to create or modify anything in Blender.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured
- Priorität: high

### 9. concept-diagrams

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/creative/concept-diagrams/SKILL.md`
- Name: concept-diagrams
- Zweck: Generate flat, minimal light/dark-aware SVG diagrams as standalone HTML files, using a unified educational visual language with 9 semantic color ramps, sentence-case typography, and automatic dark mode. Best suited for educational and non-s
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 10. hyperframes

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/creative/hyperframes/SKILL.md`
- Name: hyperframes
- Zweck: Create HTML-based video compositions, animated title cards, social overlays, captioned talking-head videos, audio-reactive visuals, and shader transitions using HyperFrames. HTML is the source of truth for video. Use when the user wants a r
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 11. kanban-video-orchestrator

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/creative/kanban-video-orchestrator/SKILL.md`
- Name: kanban-video-orchestrator
- Zweck: Plan, set up, and monitor a multi-agent video production pipeline backed by Hermes Kanban. Use when the user wants to make ANY video — narrative film, product/marketing, music video, explainer, ASCII/terminal art, abstract/generative loop,
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 12. meme-generation

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/creative/meme-generation/SKILL.md`
- Name: meme-generation
- Zweck: Generate real meme images by picking a template and overlaying text with Pillow. Produces actual .png meme files.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 13. inference-sh-cli

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/devops/cli/SKILL.md`
- Name: inference-sh-cli
- Zweck: Run 150+ AI apps via inference.sh CLI (infsh) — image generation, video creation, LLMs, search, 3D, social automation. Uses the terminal tool. Triggers: inference.sh, infsh, ai apps, flux, veo, image generation, video generation, seedream,
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 14. docker-management

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/devops/docker-management/SKILL.md`
- Name: docker-management
- Zweck: Manage Docker containers, images, volumes, networks, and Compose stacks — lifecycle ops, debugging, cleanup, and Dockerfile optimization.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 15. pinggy-tunnel

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/devops/pinggy-tunnel/SKILL.md`
- Name: pinggy-tunnel
- Zweck: Zero-install localhost tunnels over SSH via Pinggy.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 16. watchers

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/devops/watchers/SKILL.md`
- Name: watchers
- Zweck: Poll RSS, JSON APIs, and GitHub with watermark dedup.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 17. adversarial-ux-test

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/dogfood/adversarial-ux-test/SKILL.md`
- Name: adversarial-ux-test
- Zweck: Roleplay the most difficult, tech-resistant user for your product. Browse the app as that persona, find every UX pain point, then filter complaints through a pragmatism layer to separate real problems from noise. Creates actionable tickets
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 18. agentmail

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/email/agentmail/SKILL.md`
- Name: agentmail
- Zweck: Give the agent its own dedicated email inbox via AgentMail. Send, receive, and manage email autonomously using agent-owned email addresses (e.g. hermes-agent@agentmail.to).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 19. 3-statement-model

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/3-statement-model/SKILL.md`
- Name: 3-statement-model
- Zweck: Build fully-integrated 3-statement models (IS, BS, CF) in Excel with working capital schedules, D&A roll-forwards, debt schedule, and the plugs that make cash and retained earnings tie. Pairs with excel-author.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 20. comps-analysis

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/comps-analysis/SKILL.md`
- Name: comps-analysis
- Zweck: Build comparable company analysis in Excel — operating metrics, valuation multiples, statistical benchmarking vs peer sets. Pairs with excel-author. Use for public-company valuation, IPO pricing, sector benchmarking, or outlier detection.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 21. dcf-model

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/dcf-model/SKILL.md`
- Name: dcf-model
- Zweck: Build institutional-quality DCF valuation models in Excel — revenue projections, FCF build, WACC, terminal value, Bear/Base/Bull scenarios, 5x5 sensitivity tables. Pairs with excel-author. Use for intrinsic-value equity analysis.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 22. excel-author

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/excel-author/SKILL.md`
- Name: excel-author
- Zweck: Build auditable Excel workbooks headless with openpyxl — blue/black/green cell conventions, formulas over hardcodes, named ranges, balance checks, sensitivity tables. Use for financial models, audit outputs, reconciliations.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 23. lbo-model

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/lbo-model/SKILL.md`
- Name: lbo-model
- Zweck: Build leveraged buyout models in Excel — sources & uses, debt schedule, cash sweep, exit multiple, IRR/MOIC sensitivity. Pairs with excel-author. Use for PE screening, sponsor-case valuation, or illustrative LBO in a pitch.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 24. merger-model

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/merger-model/SKILL.md`
- Name: merger-model
- Zweck: Build accretion/dilution (merger) models in Excel — pro-forma P&L, synergies, financing mix, EPS impact. Pairs with excel-author. Use for M&A pitches, board materials, or deal evaluation.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 25. pptx-author

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/pptx-author/SKILL.md`
- Name: pptx-author
- Zweck: Build PowerPoint decks headless with python-pptx. Pairs with excel-author for model-backed decks where every number traces to a workbook cell. Use for pitch decks, IC memos, earnings notes.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured
- Priorität: low

### 26. stocks

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/finance/stocks/SKILL.md`
- Name: stocks
- Zweck: Stock quotes, history, search, compare, crypto via Yahoo.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 27. fitness-nutrition

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/health/fitness-nutrition/SKILL.md`
- Name: fitness-nutrition
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 28. neuroskill-bci

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/health/neuroskill-bci/SKILL.md`
- Name: neuroskill-bci
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 29. fastmcp

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mcp/fastmcp/SKILL.md`
- Name: fastmcp
- Zweck: Build, test, inspect, install, and deploy MCP servers with FastMCP in Python. Use when creating a new MCP server, wrapping an API or database as MCP tools, exposing resources or prompts, or preparing a FastMCP server for Claude Code, Cursor
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 30. mcporter

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mcp/mcporter/SKILL.md`
- Name: mcporter
- Zweck: Use the mcporter CLI to list, configure, auth, and call MCP servers/tools directly (HTTP or stdio), including ad-hoc servers, config edits, and CLI/type generation.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured; Evalability unclear
- Priorität: medium

### 31. openclaw-migration

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/migration/openclaw-migration/SKILL.md`
- Name: openclaw-migration
- Zweck: Migrate a user's OpenClaw customization footprint into Hermes Agent. Imports Hermes-compatible memories, SOUL.md, command allowlists, user skills, and selected workspace assets from ~/.openclaw, then reports exactly what could not be migrat
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 32. huggingface-accelerate

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/accelerate/SKILL.md`
- Name: huggingface-accelerate
- Zweck: Simplest distributed training API. 4 lines to add distributed support to any PyTorch script. Unified API for DeepSpeed/FSDP/Megatron/DDP. Automatic device placement, mixed precision (FP16/BF16/FP8). Interactive config, single launch command
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 33. chroma

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/chroma/SKILL.md`
- Name: chroma
- Zweck: Open-source embedding database for AI applications. Store embeddings and metadata, perform vector and full-text search, filter by metadata. Simple 4-function API. Scales from notebooks to production clusters. Use for semantic search, RAG ap
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 34. clip

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/clip/SKILL.md`
- Name: clip
- Zweck: OpenAI's model connecting vision and language. Enables zero-shot image classification, image-text matching, and cross-modal retrieval. Trained on 400M image-text pairs. Use for image search, content moderation, or vision-language tasks with
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 35. faiss

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/faiss/SKILL.md`
- Name: faiss
- Zweck: Facebook's library for efficient similarity search and clustering of dense vectors. Supports billions of vectors, GPU acceleration, and various index types (Flat, IVF, HNSW). Use for fast k-NN search, large-scale vector retrieval, or when y
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 36. optimizing-attention-flash

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/flash-attention/SKILL.md`
- Name: optimizing-attention-flash
- Zweck: Optimizes transformer attention with Flash Attention for 2-4x speedup and 10-20x memory reduction. Use when training/running transformers with long sequences (>512 tokens), encountering GPU memory issues with attention, or need faster infer
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 37. guidance

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/guidance/SKILL.md`
- Name: guidance
- Zweck: Control LLM output with regex and grammars, guarantee valid JSON/XML/code generation, enforce structured formats, and build multi-step workflows with Guidance - Microsoft Research's constrained generation framework
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 38. huggingface-tokenizers

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/huggingface-tokenizers/SKILL.md`
- Name: huggingface-tokenizers
- Zweck: Fast tokenizers optimized for research and production. Rust-based implementation tokenizes 1GB in <20 seconds. Supports BPE, WordPiece, and Unigram algorithms. Train custom vocabularies, track alignments, handle padding/truncation. Integrat
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured
- Priorität: high

### 39. outlines

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/inference/outlines/SKILL.md`
- Name: outlines
- Zweck: Outlines: structured JSON/regex/Pydantic LLM generation.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 40. instructor

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/instructor/SKILL.md`
- Name: instructor
- Zweck: Extract structured data from LLM responses with Pydantic validation, retry failed extractions automatically, parse complex JSON with type safety, and stream partial results with Instructor - battle-tested structured output library
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 41. lambda-labs-gpu-cloud

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/lambda-labs/SKILL.md`
- Name: lambda-labs-gpu-cloud
- Zweck: Reserved and on-demand GPU cloud instances for ML training and inference. Use when you need dedicated GPU instances with simple SSH access, persistent filesystems, or high-performance multi-node clusters for large-scale training.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 42. llava

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/llava/SKILL.md`
- Name: llava
- Zweck: Large Language and Vision Assistant. Enables visual instruction tuning and image-based conversations. Combines CLIP vision encoder with Vicuna/LLaMA language models. Supports multi-turn image chat, visual question answering, and instruction
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 43. modal-serverless-gpu

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/modal/SKILL.md`
- Name: modal-serverless-gpu
- Zweck: Serverless GPU cloud platform for running ML workloads. Use when you need on-demand GPU access without infrastructure management, deploying ML models as APIs, or running batch jobs with automatic scaling.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 44. nemo-curator

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/nemo-curator/SKILL.md`
- Name: nemo-curator
- Zweck: GPU-accelerated data curation for LLM training. Supports text/image/video/audio. Features fuzzy deduplication (16× faster), quality filtering (30+ heuristics), semantic deduplication, PII redaction, NSFW detection. Scales across GPUs with R
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured
- Priorität: high

### 45. peft-fine-tuning

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/peft/SKILL.md`
- Name: peft-fine-tuning
- Zweck: Parameter-efficient fine-tuning for LLMs using LoRA, QLoRA, and 25+ methods. Use when fine-tuning large models (7B-70B) with limited GPU memory, when you need to train <1% of parameters with minimal accuracy loss, or for multi-adapter servi
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 46. pinecone

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/pinecone/SKILL.md`
- Name: pinecone
- Zweck: Managed vector database for production AI applications. Fully managed, auto-scaling, with hybrid search (dense + sparse), metadata filtering, and namespaces. Low latency (<100ms p95). Use for production RAG, recommendation systems, or seman
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 47. pytorch-fsdp

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/pytorch-fsdp/SKILL.md`
- Name: pytorch-fsdp
- Zweck: Expert guidance for Fully Sharded Data Parallel training with PyTorch FSDP - parameter sharding, mixed precision, CPU offloading, FSDP2
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 48. pytorch-lightning

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/pytorch-lightning/SKILL.md`
- Name: pytorch-lightning
- Zweck: High-level PyTorch framework with Trainer class, automatic distributed training (DDP/FSDP/DeepSpeed), callbacks system, and minimal boilerplate. Scales from laptop to supercomputer with same code. Use when you want clean training loops with
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 49. qdrant-vector-search

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/qdrant/SKILL.md`
- Name: qdrant-vector-search
- Zweck: High-performance vector similarity search engine for RAG and semantic search. Use when building production RAG systems requiring fast nearest neighbor search, hybrid search with filtering, or scalable vector storage with Rust-powered perfor
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing; Workflow insufficiently structured
- Priorität: high

### 50. sparse-autoencoder-training

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/saelens/SKILL.md`
- Name: sparse-autoencoder-training
- Zweck: Provides guidance for training and analyzing Sparse Autoencoders (SAEs) using SAELens to decompose neural network activations into interpretable features. Use when discovering interpretable features, analyzing superposition, or studying mon
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 51. simpo-training

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/simpo/SKILL.md`
- Name: simpo-training
- Zweck: Simple Preference Optimization for LLM alignment. Reference-free alternative to DPO with better performance (+6.4 points on AlpacaEval 2.0). No reference model needed, more efficient than DPO. Use for preference alignment when want simpler,
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 52. slime-rl-training

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/slime/SKILL.md`
- Name: slime-rl-training
- Zweck: Provides guidance for LLM post-training with RL using slime, a Megatron+SGLang framework. Use when training GLM models, implementing custom data generation workflows, or needing tight Megatron-LM integration for RL scaling.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 53. stable-diffusion-image-generation

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/stable-diffusion/SKILL.md`
- Name: stable-diffusion-image-generation
- Zweck: State-of-the-art text-to-image generation with Stable Diffusion models via HuggingFace Diffusers. Use when generating images from text prompts, performing image-to-image translation, inpainting, or building custom diffusion pipelines.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 54. tensorrt-llm

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/tensorrt-llm/SKILL.md`
- Name: tensorrt-llm
- Zweck: Optimizes LLM inference with NVIDIA TensorRT for maximum throughput and lowest latency. Use for production deployment on NVIDIA GPUs (A100/H100), when you need 10-100x faster inference than PyTorch, or for serving models with quantization (
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 55. distributed-llm-pretraining-torchtitan

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/torchtitan/SKILL.md`
- Name: distributed-llm-pretraining-torchtitan
- Zweck: Provides PyTorch-native distributed LLM pretraining using torchtitan with 4D parallelism (FSDP2, TP, PP, CP). Use when pretraining Llama 3.1, DeepSeek V3, or custom models at scale from 8 to 512+ GPUs with Float8, torch.compile, and distrib
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 56. axolotl

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/training/axolotl/SKILL.md`
- Name: axolotl
- Zweck: Axolotl: YAML LLM fine-tuning (LoRA, DPO, GRPO).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 57. fine-tuning-with-trl

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/training/trl-fine-tuning/SKILL.md`
- Name: fine-tuning-with-trl
- Zweck: TRL: SFT, DPO, PPO, GRPO, reward modeling for LLM RLHF.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 58. unsloth

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/training/unsloth/SKILL.md`
- Name: unsloth
- Zweck: Unsloth: 2-5x faster LoRA/QLoRA fine-tuning, less VRAM.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 59. whisper

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/mlops/whisper/SKILL.md`
- Name: whisper
- Zweck: OpenAI's general-purpose speech recognition model. Supports 99 languages, transcription, translation to English, and language identification. Six model sizes from tiny (39M params) to large (1550M params). Use for speech-to-text, podcast tr
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 60. canvas

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/canvas/SKILL.md`
- Name: canvas
- Zweck: Canvas LMS integration — fetch enrolled courses and assignments using API token authentication.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 61. here.now

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/here-now/SKILL.md`
- Name: here.now
- Zweck: Publish static sites to {slug}.here.now and store private files in cloud Drives for agent-to-agent handoff.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 62. memento-flashcards

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/memento-flashcards/SKILL.md`
- Name: memento-flashcards
- Zweck: >-
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 63. shop-app

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/shop-app/SKILL.md`
- Name: shop-app
- Zweck: Shop.app: product search, order tracking, returns, reorder.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 64. shopify

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/shopify/SKILL.md`
- Name: shopify
- Zweck: Shopify Admin & Storefront GraphQL APIs via curl. Products, orders, customers, inventory, metafields.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 65. siyuan

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/siyuan/SKILL.md`
- Name: siyuan
- Zweck: SiYuan Note API for searching, reading, creating, and managing blocks and documents in a self-hosted knowledge base via curl.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 66. telephony

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/productivity/telephony/SKILL.md`
- Name: telephony
- Zweck: Give Hermes phone capabilities without core tool changes. Provision and persist a Twilio number, send and receive SMS/MMS, make direct calls, and place AI-driven outbound calls through Bland.ai or Vapi.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Output contract missing
- Priorität: high

### 67. bioinformatics

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/bioinformatics/SKILL.md`
- Name: bioinformatics
- Zweck: Gateway to 400+ bioinformatics skills from bioSkills and ClawBio. Covers genomics, transcriptomics, single-cell, variant calling, pharmacogenomics, metagenomics, structural biology, and more. Fetches domain-specific reference material on de
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 68. darwinian-evolver

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/darwinian-evolver/SKILL.md`
- Name: darwinian-evolver
- Zweck: Evolve prompts/regex/SQL/code with Imbue's evolution loop.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 69. domain-intel

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/domain-intel/SKILL.md`
- Name: domain-intel
- Zweck: Passive domain reconnaissance using Python stdlib. Subdomain discovery, SSL certificate inspection, WHOIS lookups, DNS records, domain availability checks, and bulk multi-domain analysis. No API keys required.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 70. drug-discovery

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/drug-discovery/SKILL.md`
- Name: drug-discovery
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 71. duckduckgo-search

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/duckduckgo-search/SKILL.md`
- Name: duckduckgo-search
- Zweck: Free web search via DuckDuckGo — text, news, images, videos. No API key needed. Prefer the `ddgs` CLI when installed; use the Python DDGS library only after verifying that `ddgs` is available in the current runtime.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 72. gitnexus-explorer

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/gitnexus-explorer/SKILL.md`
- Name: gitnexus-explorer
- Zweck: Index a codebase with GitNexus and serve an interactive knowledge graph via web UI + Cloudflare tunnel.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Output contract missing; Examples missing
- Priorität: high

### 73. osint-investigation

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/osint-investigation/SKILL.md`
- Name: osint-investigation
- Zweck: Public-records OSINT investigation framework — SEC EDGAR filings, USAspending contracts, Senate lobbying, OFAC sanctions, ICIJ offshore leaks, NYC property records (ACRIS), OpenCorporates registries, CourtListener court records, Wayback Mac
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 74. parallel-cli

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/parallel-cli/SKILL.md`
- Name: parallel-cli
- Zweck: Optional vendor skill for Parallel CLI — agent-native web search, extraction, deep research, enrichment, FindAll, and monitoring. Prefer JSON output and non-interactive flows.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 75. qmd

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/qmd/SKILL.md`
- Name: qmd
- Zweck: Search personal knowledge bases, notes, docs, and meeting transcripts locally using qmd — a hybrid retrieval engine with BM25, vector search, and LLM reranking. Supports CLI and MCP integration.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 76. scrapling

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/scrapling/SKILL.md`
- Name: scrapling
- Zweck: Web scraping with Scrapling - HTTP fetching, stealth browser automation, Cloudflare bypass, and spider crawling via CLI and Python.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 77. searxng-search

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/research/searxng-search/SKILL.md`
- Name: searxng-search
- Zweck: Free meta-search via SearXNG — aggregates results from 70+ search engines. Self-hosted or use a public instance. No API key needed. Falls back automatically when the web search toolset is unavailable.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 78. 1password

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/security/1password/SKILL.md`
- Name: 1password
- Zweck: Set up and use 1Password CLI (op). Use when installing the CLI, enabling desktop app integration, signing in, and reading/injecting secrets for commands.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 79. oss-forensics

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/security/oss-forensics/SKILL.md`
- Name: oss-forensics
- Zweck: |
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 80. sherlock

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/security/sherlock/SKILL.md`
- Name: sherlock
- Zweck: OSINT username search across 400+ social networks. Hunt down social media accounts by username.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 81. web-pentest

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/security/web-pentest/SKILL.md`
- Name: web-pentest
- Zweck: |
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 82. code-wiki

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/software-development/code-wiki/SKILL.md`
- Name: code-wiki
- Zweck: Generate wiki docs + Mermaid diagrams for any codebase.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 83. rest-graphql-debug

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/software-development/rest-graphql-debug/SKILL.md`
- Name: rest-graphql-debug
- Zweck: Debug REST/GraphQL APIs: status codes, auth, schemas, repro.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 84. page-agent

- Pfad: `/home/piet/.hermes/hermes-agent/optional-skills/web-development/page-agent/SKILL.md`
- Name: page-agent
- Zweck: Embed alibaba/page-agent into your own web application — a pure-JavaScript in-page GUI agent that ships as a single <script> tag or npm package and lets end-users of your site drive the UI with natural language ("click login, fill username
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 85. apple-notes

- Pfad: `/home/piet/.hermes/hermes-agent/skills/apple/apple-notes/SKILL.md`
- Name: apple-notes
- Zweck: Manage Apple Notes via memo CLI: create, search, edit.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 86. apple-reminders

- Pfad: `/home/piet/.hermes/hermes-agent/skills/apple/apple-reminders/SKILL.md`
- Name: apple-reminders
- Zweck: Apple Reminders via remindctl: add, list, complete.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 87. findmy

- Pfad: `/home/piet/.hermes/hermes-agent/skills/apple/findmy/SKILL.md`
- Name: findmy
- Zweck: Track Apple devices/AirTags via FindMy.app on macOS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Output contract missing; Examples missing
- Priorität: high

### 88. imessage

- Pfad: `/home/piet/.hermes/hermes-agent/skills/apple/imessage/SKILL.md`
- Name: imessage
- Zweck: Send and receive iMessages/SMS via the imsg CLI on macOS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 89. macos-computer-use

- Pfad: `/home/piet/.hermes/hermes-agent/skills/apple/macos-computer-use/SKILL.md`
- Name: macos-computer-use
- Zweck: |
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Output contract missing
- Priorität: high

### 90. claude-code

- Pfad: `/home/piet/.hermes/hermes-agent/skills/autonomous-ai-agents/claude-code/SKILL.md`
- Name: claude-code
- Zweck: Delegate coding to Claude Code CLI (features, PRs).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 91. codex

- Pfad: `/home/piet/.hermes/hermes-agent/skills/autonomous-ai-agents/codex/SKILL.md`
- Name: codex
- Zweck: Delegate coding to OpenAI Codex CLI (features, PRs).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing; Workflow insufficiently structured
- Priorität: high

### 92. hermes-agent

- Pfad: `/home/piet/.hermes/hermes-agent/skills/autonomous-ai-agents/hermes-agent/SKILL.md`
- Name: hermes-agent
- Zweck: Configure, extend, or contribute to Hermes Agent.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 93. kanban-codex-lane

- Pfad: `/home/piet/.hermes/hermes-agent/skills/autonomous-ai-agents/kanban-codex-lane/SKILL.md`
- Name: kanban-codex-lane
- Zweck: Use when a Hermes Kanban worker wants to run Codex CLI as an isolated implementation lane while Hermes keeps ownership of task lifecycle, reconciliation, testing, and handoff.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 94. opencode

- Pfad: `/home/piet/.hermes/hermes-agent/skills/autonomous-ai-agents/opencode/SKILL.md`
- Name: opencode
- Zweck: Delegate coding to OpenCode CLI (features, PR review).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 95. architecture-diagram

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/architecture-diagram/SKILL.md`
- Name: architecture-diagram
- Zweck: Dark-themed SVG architecture/cloud/infra diagrams as HTML.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 96. ascii-art

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/ascii-art/SKILL.md`
- Name: ascii-art
- Zweck: ASCII art: pyfiglet, cowsay, boxes, image-to-ascii.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 97. ascii-video

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/ascii-video/SKILL.md`
- Name: ascii-video
- Zweck: ASCII video: convert video/audio to colored ASCII MP4/GIF.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 98. baoyu-article-illustrator

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/baoyu-article-illustrator/SKILL.md`
- Name: baoyu-article-illustrator
- Zweck: Article illustrations: type × style × palette consistency.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low

### 99. baoyu-comic

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/baoyu-comic/SKILL.md`
- Name: baoyu-comic
- Zweck: Knowledge comics (知识漫画): educational, biography, tutorial.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 100. baoyu-infographic

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/baoyu-infographic/SKILL.md`
- Name: baoyu-infographic
- Zweck: Infographics: 21 layouts x 21 styles (信息图, 可视化).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 101. claude-design

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/claude-design/SKILL.md`
- Name: claude-design
- Zweck: Design one-off HTML artifacts (landing, deck, prototype).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 102. comfyui

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/comfyui/SKILL.md`
- Name: comfyui
- Zweck: Generate images, video, and audio with ComfyUI — install, launch, manage nodes/models, run workflows with parameter injection. Uses the official comfy-cli for lifecycle and direct REST/WebSocket API for execution.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 103. ideation

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/creative-ideation/SKILL.md`
- Name: ideation
- Zweck: Generate project ideas via creative constraints.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 104. design-md

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/design-md/SKILL.md`
- Name: design-md
- Zweck: Author/validate/export Google's DESIGN.md token spec files.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 105. excalidraw

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/excalidraw/SKILL.md`
- Name: excalidraw
- Zweck: Hand-drawn Excalidraw JSON diagrams (arch, flow, seq).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low

### 106. humanizer

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/humanizer/SKILL.md`
- Name: humanizer
- Zweck: Humanize text: strip AI-isms and add real voice.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 107. manim-video

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/manim-video/SKILL.md`
- Name: manim-video
- Zweck: Manim CE animations: 3Blue1Brown math/algo videos.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 108. p5js

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/p5js/SKILL.md`
- Name: p5js
- Zweck: p5.js sketches: gen art, shaders, interactive, 3D.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 109. pixel-art

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/pixel-art/SKILL.md`
- Name: pixel-art
- Zweck: Pixel art w/ era palettes (NES, Game Boy, PICO-8).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Evalability unclear
- Priorität: low

### 110. popular-web-designs

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/popular-web-designs/SKILL.md`
- Name: popular-web-designs
- Zweck: 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 111. pretext

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/pretext/SKILL.md`
- Name: pretext
- Zweck: Use when building creative browser demos with @chenglou/pretext — DOM-free text layout for ASCII art, typographic flow around obstacles, text-as-geometry games, kinetic typography, and text-powered generative art. Produces single-file HTML
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 112. sketch

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/sketch/SKILL.md`
- Name: sketch
- Zweck: Throwaway HTML mockups: 2-3 design variants to compare.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 113. songwriting-and-ai-music

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/songwriting-and-ai-music/SKILL.md`
- Name: songwriting-and-ai-music
- Zweck: Songwriting craft and Suno AI music prompts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 114. touchdesigner-mcp

- Pfad: `/home/piet/.hermes/hermes-agent/skills/creative/touchdesigner-mcp/SKILL.md`
- Name: touchdesigner-mcp
- Zweck: Control a running TouchDesigner instance via twozero MCP — create operators, set parameters, wire connections, execute Python, build real-time visuals. 36 native tools.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 115. jupyter-live-kernel

- Pfad: `/home/piet/.hermes/hermes-agent/skills/data-science/jupyter-live-kernel/SKILL.md`
- Name: jupyter-live-kernel
- Zweck: Iterative Python via live Jupyter kernel (hamelnb).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 116. kanban-orchestrator

- Pfad: `/home/piet/.hermes/hermes-agent/skills/devops/kanban-orchestrator/SKILL.md`
- Name: kanban-orchestrator
- Zweck: Decomposition playbook + anti-temptation rules for an orchestrator profile routing work through Kanban. The "don't do the work yourself" rule and the basic lifecycle are auto-injected into every kanban worker's system prompt; this skill is
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 117. kanban-worker

- Pfad: `/home/piet/.hermes/hermes-agent/skills/devops/kanban-worker/SKILL.md`
- Name: kanban-worker
- Zweck: Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 118. webhook-subscriptions

- Pfad: `/home/piet/.hermes/hermes-agent/skills/devops/webhook-subscriptions/SKILL.md`
- Name: webhook-subscriptions
- Zweck: Webhook subscriptions: event-driven agent runs.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 119. dogfood

- Pfad: `/home/piet/.hermes/hermes-agent/skills/dogfood/SKILL.md`
- Name: dogfood
- Zweck: Exploratory QA of web apps: find bugs, evidence, reports.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 120. himalaya

- Pfad: `/home/piet/.hermes/hermes-agent/skills/email/himalaya/SKILL.md`
- Name: himalaya
- Zweck: Himalaya CLI: IMAP/SMTP email from terminal.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 121. minecraft-modpack-server

- Pfad: `/home/piet/.hermes/hermes-agent/skills/gaming/minecraft-modpack-server/SKILL.md`
- Name: minecraft-modpack-server
- Zweck: Host modded Minecraft servers (CurseForge, Modrinth).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing
- Priorität: high

### 122. pokemon-player

- Pfad: `/home/piet/.hermes/hermes-agent/skills/gaming/pokemon-player/SKILL.md`
- Name: pokemon-player
- Zweck: Play Pokemon via headless emulator + RAM reads.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 123. codebase-inspection

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/codebase-inspection/SKILL.md`
- Name: codebase-inspection
- Zweck: Inspect codebases w/ pygount: LOC, languages, ratios.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 124. github-auth

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/github-auth/SKILL.md`
- Name: github-auth
- Zweck: GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 125. github-code-review

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/github-code-review/SKILL.md`
- Name: github-code-review
- Zweck: Review PRs: diffs, inline comments via gh or REST.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 126. github-issues

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/github-issues/SKILL.md`
- Name: github-issues
- Zweck: Create, triage, label, assign GitHub issues via gh or REST.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 127. github-pr-workflow

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/github-pr-workflow/SKILL.md`
- Name: github-pr-workflow
- Zweck: GitHub PR lifecycle: branch, commit, open, CI, merge.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 128. github-repo-management

- Pfad: `/home/piet/.hermes/hermes-agent/skills/github/github-repo-management/SKILL.md`
- Name: github-repo-management
- Zweck: Clone/create/fork repos; manage remotes, releases.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 129. native-mcp

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mcp/native-mcp/SKILL.md`
- Name: native-mcp
- Zweck: MCP client: connect servers, register tools (stdio/HTTP).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 130. gif-search

- Pfad: `/home/piet/.hermes/hermes-agent/skills/media/gif-search/SKILL.md`
- Name: gif-search
- Zweck: Search/download GIFs from Tenor via curl + jq.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: low

### 131. heartmula

- Pfad: `/home/piet/.hermes/hermes-agent/skills/media/heartmula/SKILL.md`
- Name: heartmula
- Zweck: HeartMuLa: Suno-like song generation from lyrics + tags.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 132. songsee

- Pfad: `/home/piet/.hermes/hermes-agent/skills/media/songsee/SKILL.md`
- Name: songsee
- Zweck: Audio spectrograms/features (mel, chroma, MFCC) via CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured
- Priorität: high

### 133. spotify

- Pfad: `/home/piet/.hermes/hermes-agent/skills/media/spotify/SKILL.md`
- Name: spotify
- Zweck: Spotify: play, search, queue, manage playlists and devices.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: low

### 134. youtube-content

- Pfad: `/home/piet/.hermes/hermes-agent/skills/media/youtube-content/SKILL.md`
- Name: youtube-content
- Zweck: YouTube transcripts to summaries, threads, blogs.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 135. evaluating-llms-harness

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/evaluation/lm-evaluation-harness/SKILL.md`
- Name: evaluating-llms-harness
- Zweck: lm-eval-harness: benchmark LLMs (MMLU, GSM8K, etc.).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 136. weights-and-biases

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/evaluation/weights-and-biases/SKILL.md`
- Name: weights-and-biases
- Zweck: W&B: log ML experiments, sweeps, model registry, dashboards.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 137. huggingface-hub

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/huggingface-hub/SKILL.md`
- Name: huggingface-hub
- Zweck: HuggingFace hf CLI: search/download/upload models, datasets.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 138. llama-cpp

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/inference/llama-cpp/SKILL.md`
- Name: llama-cpp
- Zweck: llama.cpp local GGUF inference + HF Hub model discovery.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 139. obliteratus

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/inference/obliteratus/SKILL.md`
- Name: obliteratus
- Zweck: OBLITERATUS: abliterate LLM refusals (diff-in-means).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 140. serving-llms-vllm

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/inference/vllm/SKILL.md`
- Name: serving-llms-vllm
- Zweck: vLLM: high-throughput LLM serving, OpenAI API, quantization.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 141. audiocraft-audio-generation

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/models/audiocraft/SKILL.md`
- Name: audiocraft-audio-generation
- Zweck: AudioCraft: MusicGen text-to-music, AudioGen text-to-sound.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 142. segment-anything-model

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/models/segment-anything/SKILL.md`
- Name: segment-anything-model
- Zweck: SAM: zero-shot image segmentation via points, boxes, masks.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 143. dspy

- Pfad: `/home/piet/.hermes/hermes-agent/skills/mlops/research/dspy/SKILL.md`
- Name: dspy
- Zweck: DSPy: declarative LM programs, auto-optimize prompts, RAG.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 144. obsidian

- Pfad: `/home/piet/.hermes/hermes-agent/skills/note-taking/obsidian/SKILL.md`
- Name: obsidian
- Zweck: Read, search, create, and edit notes in the Obsidian vault.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Output contract missing
- Priorität: high

### 145. airtable

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/airtable/SKILL.md`
- Name: airtable
- Zweck: Airtable REST API via curl. Records CRUD, filters, upserts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 146. google-workspace

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/google-workspace/SKILL.md`
- Name: google-workspace
- Zweck: Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 147. linear

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/linear/SKILL.md`
- Name: linear
- Zweck: Linear: manage issues, projects, teams via GraphQL + curl.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 148. maps

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/maps/SKILL.md`
- Name: maps
- Zweck: Geocode, POIs, routes, timezones via OpenStreetMap/OSRM.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 149. nano-pdf

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/nano-pdf/SKILL.md`
- Name: nano-pdf
- Zweck: Edit PDF text/typos/titles via nano-pdf CLI (NL prompts).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 150. notion

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/notion/SKILL.md`
- Name: notion
- Zweck: Notion API + ntn CLI: pages, databases, markdown, Workers.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 151. ocr-and-documents

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/ocr-and-documents/SKILL.md`
- Name: ocr-and-documents
- Zweck: Extract text from PDFs/scans (pymupdf, marker-pdf).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 152. powerpoint

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/powerpoint/SKILL.md`
- Name: powerpoint
- Zweck: Create, read, edit .pptx decks, slides, notes, templates.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 153. teams-meeting-pipeline

- Pfad: `/home/piet/.hermes/hermes-agent/skills/productivity/teams-meeting-pipeline/SKILL.md`
- Name: teams-meeting-pipeline
- Zweck: Operate the Teams meeting summary pipeline via Hermes CLI — summarize meetings, inspect pipeline status, replay jobs, manage Microsoft Graph subscriptions.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 154. godmode

- Pfad: `/home/piet/.hermes/hermes-agent/skills/red-teaming/godmode/SKILL.md`
- Name: godmode
- Zweck: Jailbreak LLMs: Parseltongue, GODMODE, ULTRAPLINIAN.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 155. arxiv

- Pfad: `/home/piet/.hermes/hermes-agent/skills/research/arxiv/SKILL.md`
- Name: arxiv
- Zweck: Search arXiv papers by keyword, author, category, or ID.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 156. blogwatcher

- Pfad: `/home/piet/.hermes/hermes-agent/skills/research/blogwatcher/SKILL.md`
- Name: blogwatcher
- Zweck: Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 157. llm-wiki

- Pfad: `/home/piet/.hermes/hermes-agent/skills/research/llm-wiki/SKILL.md`
- Name: llm-wiki
- Zweck: Karpathy's LLM Wiki: build/query interlinked markdown KB.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 158. polymarket

- Pfad: `/home/piet/.hermes/hermes-agent/skills/research/polymarket/SKILL.md`
- Name: polymarket
- Zweck: Query Polymarket: markets, prices, orderbooks, history.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 159. research-paper-writing

- Pfad: `/home/piet/.hermes/hermes-agent/skills/research/research-paper-writing/SKILL.md`
- Name: research-paper-writing
- Zweck: Write ML papers for NeurIPS/ICML/ICLR: design→submit.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 160. openhue

- Pfad: `/home/piet/.hermes/hermes-agent/skills/smart-home/openhue/SKILL.md`
- Name: openhue
- Zweck: Control Philips Hue lights, scenes, rooms via OpenHue CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 161. xurl

- Pfad: `/home/piet/.hermes/hermes-agent/skills/social-media/xurl/SKILL.md`
- Name: xurl
- Zweck: X/Twitter via xurl CLI: post, search, DM, media, v2 API.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 162. debugging-hermes-tui-commands

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/debugging-hermes-tui-commands/SKILL.md`
- Name: debugging-hermes-tui-commands
- Zweck: Debug Hermes TUI slash commands: Python, gateway, Ink UI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 163. hermes-agent-skill-authoring

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/hermes-agent-skill-authoring/SKILL.md`
- Name: hermes-agent-skill-authoring
- Zweck: Author in-repo SKILL.md: frontmatter, validator, structure.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing
- Priorität: high

### 164. hermes-s6-container-supervision

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/hermes-s6-container-supervision/SKILL.md`
- Name: hermes-s6-container-supervision
- Zweck: Modify, debug, or extend the s6-overlay supervision tree inside the Hermes Agent Docker image — adding new services, debugging profile gateways, understanding the Architecture B main-program pattern.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured
- Priorität: low

### 165. node-inspect-debugger

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/node-inspect-debugger/SKILL.md`
- Name: node-inspect-debugger
- Zweck: Debug Node.js via --inspect + Chrome DevTools Protocol CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 166. plan

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/plan/SKILL.md`
- Name: plan
- Zweck: Plan mode: write markdown plan to .hermes/plans/, no exec.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 167. python-debugpy

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/python-debugpy/SKILL.md`
- Name: python-debugpy
- Zweck: Debug Python: pdb REPL + debugpy remote (DAP).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing; Workflow insufficiently structured
- Priorität: high

### 168. requesting-code-review

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/requesting-code-review/SKILL.md`
- Name: requesting-code-review
- Zweck: Pre-commit review: security scan, quality gates, auto-fix.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 169. spike

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/spike/SKILL.md`
- Name: spike
- Zweck: Throwaway experiments to validate an idea before build.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 170. subagent-driven-development

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/subagent-driven-development/SKILL.md`
- Name: subagent-driven-development
- Zweck: Execute plans via delegate_task subagents (2-stage review).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 171. systematic-debugging

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/systematic-debugging/SKILL.md`
- Name: systematic-debugging
- Zweck: 4-phase root cause debugging: understand bugs before fixing.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 172. test-driven-development

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/test-driven-development/SKILL.md`
- Name: test-driven-development
- Zweck: TDD: enforce RED-GREEN-REFACTOR, tests before code.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 173. writing-plans

- Pfad: `/home/piet/.hermes/hermes-agent/skills/software-development/writing-plans/SKILL.md`
- Name: writing-plans
- Zweck: Write implementation plans: bite-sized tasks, paths, code.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 174. yuanbao

- Pfad: `/home/piet/.hermes/hermes-agent/skills/yuanbao/SKILL.md`
- Name: yuanbao
- Zweck: Yuanbao (元宝) groups: @mention users, query info/members.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low

### 175. adversarial-python-security

- Pfad: `/home/piet/.hermes/skills/.archive/devops/adversarial-python-security/SKILL.md`
- Name: adversarial-python-security
- Zweck: Red-team testing and security hardening for Python modules. Exploit simulation, path-sanitisation, symlink-safety, env-hardening.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 176. api-endpoint-hardening

- Pfad: `/home/piet/.hermes/skills/.archive/devops/api-endpoint-hardening/SKILL.md`
- Name: api-endpoint-hardening
- Zweck: |
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 177. live-evidence-verification

- Pfad: `/home/piet/.hermes/skills/.archive/devops/live-evidence-verification/SKILL.md`
- Name: live-evidence-verification
- Zweck: Verify document claims against live system state before acting on them
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 178. repeated-action-pattern

- Pfad: `/home/piet/.hermes/skills/_proposed/repeated-action-pattern/SKILL.md`
- Name: repeated-action-pattern
- Zweck: Proposed skill — extracted heuristically from 3 coordinator receipts matching pattern 'repeated-action-pattern'. Move to skills/<category>/repeated-action-pattern/ after manual review.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 179. apple-notes

- Pfad: `/home/piet/.hermes/skills/apple/apple-notes/SKILL.md`
- Name: apple-notes
- Zweck: Manage Apple Notes via memo CLI: create, search, edit.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 180. apple-reminders

- Pfad: `/home/piet/.hermes/skills/apple/apple-reminders/SKILL.md`
- Name: apple-reminders
- Zweck: Apple Reminders via remindctl: add, list, complete.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 181. findmy

- Pfad: `/home/piet/.hermes/skills/apple/findmy/SKILL.md`
- Name: findmy
- Zweck: Track Apple devices/AirTags via FindMy.app on macOS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Output contract missing; Examples missing
- Priorität: high

### 182. imessage

- Pfad: `/home/piet/.hermes/skills/apple/imessage/SKILL.md`
- Name: imessage
- Zweck: Send and receive iMessages/SMS via the imsg CLI on macOS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 183. macos-computer-use

- Pfad: `/home/piet/.hermes/skills/apple/macos-computer-use/SKILL.md`
- Name: macos-computer-use
- Zweck: |
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Output contract missing
- Priorität: high

### 184. claude-code

- Pfad: `/home/piet/.hermes/skills/autonomous-ai-agents/claude-code/SKILL.md`
- Name: claude-code
- Zweck: Hotpath for delegating scoped coding, review, and repo tasks to Claude Code CLI with local evidence first, explicit live/tool scope, permission gates, compact output, and rollback discipline.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 185. codex

- Pfad: `/home/piet/.hermes/skills/autonomous-ai-agents/codex/SKILL.md`
- Name: codex
- Zweck: Delegate coding to OpenAI Codex CLI (features, PRs).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 186. hermes-agent

- Pfad: `/home/piet/.hermes/skills/autonomous-ai-agents/hermes-agent/SKILL.md`
- Name: hermes-agent
- Zweck: Configure, extend, or contribute to Hermes Agent.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 187. kanban-codex-lane

- Pfad: `/home/piet/.hermes/skills/autonomous-ai-agents/kanban-codex-lane/SKILL.md`
- Name: kanban-codex-lane
- Zweck: Use when a Hermes Kanban worker wants to run Codex CLI as an isolated implementation lane while Hermes keeps ownership of task lifecycle, reconciliation, testing, and handoff.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 188. opencode

- Pfad: `/home/piet/.hermes/skills/autonomous-ai-agents/opencode/SKILL.md`
- Name: opencode
- Zweck: Delegate coding to OpenCode CLI (features, PR review).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 189. architecture-diagram

- Pfad: `/home/piet/.hermes/skills/creative/architecture-diagram/SKILL.md`
- Name: architecture-diagram
- Zweck: Dark-themed SVG architecture/cloud/infra diagrams as HTML.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 190. ascii-art

- Pfad: `/home/piet/.hermes/skills/creative/ascii-art/SKILL.md`
- Name: ascii-art
- Zweck: ASCII art: pyfiglet, cowsay, boxes, image-to-ascii.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 191. ascii-video

- Pfad: `/home/piet/.hermes/skills/creative/ascii-video/SKILL.md`
- Name: ascii-video
- Zweck: ASCII video: convert video/audio to colored ASCII MP4/GIF.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 192. baoyu-article-illustrator

- Pfad: `/home/piet/.hermes/skills/creative/baoyu-article-illustrator/SKILL.md`
- Name: baoyu-article-illustrator
- Zweck: Article illustrations: type × style × palette consistency.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low

### 193. baoyu-comic

- Pfad: `/home/piet/.hermes/skills/creative/baoyu-comic/SKILL.md`
- Name: baoyu-comic
- Zweck: Knowledge comics (知识漫画): educational, biography, tutorial.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 194. baoyu-infographic

- Pfad: `/home/piet/.hermes/skills/creative/baoyu-infographic/SKILL.md`
- Name: baoyu-infographic
- Zweck: Infographics: 21 layouts x 21 styles (信息图, 可视化).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 195. brainstorming

- Pfad: `/home/piet/.hermes/skills/creative/brainstorming/SKILL.md`
- Name: brainstorming
- Zweck: Use when the user wants broad idea generation, option exploration, or structured brainstorming before choosing a direction.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 196. claude-design

- Pfad: `/home/piet/.hermes/skills/creative/claude-design/SKILL.md`
- Name: claude-design
- Zweck: Design one-off HTML artifacts (landing, deck, prototype).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 197. comfyui

- Pfad: `/home/piet/.hermes/skills/creative/comfyui/SKILL.md`
- Name: comfyui
- Zweck: Generate images, video, and audio with ComfyUI — install, launch, manage nodes/models, run workflows with parameter injection. Uses the official comfy-cli for lifecycle and direct REST/WebSocket API for execution.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 198. ideation

- Pfad: `/home/piet/.hermes/skills/creative/creative-ideation/SKILL.md`
- Name: ideation
- Zweck: Generate project ideas via creative constraints.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 199. design-md

- Pfad: `/home/piet/.hermes/skills/creative/design-md/SKILL.md`
- Name: design-md
- Zweck: Author/validate/export Google's DESIGN.md token spec files.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 200. excalidraw

- Pfad: `/home/piet/.hermes/skills/creative/excalidraw/SKILL.md`
- Name: excalidraw
- Zweck: Hand-drawn Excalidraw JSON diagrams (arch, flow, seq).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low

### 201. humanizer

- Pfad: `/home/piet/.hermes/skills/creative/humanizer/SKILL.md`
- Name: humanizer
- Zweck: Humanize text: strip AI-isms and add real voice.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 202. manim-video

- Pfad: `/home/piet/.hermes/skills/creative/manim-video/SKILL.md`
- Name: manim-video
- Zweck: Manim CE animations: 3Blue1Brown math/algo videos.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 203. p5js

- Pfad: `/home/piet/.hermes/skills/creative/p5js/SKILL.md`
- Name: p5js
- Zweck: p5.js sketches: gen art, shaders, interactive, 3D.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 204. pixel-art

- Pfad: `/home/piet/.hermes/skills/creative/pixel-art/SKILL.md`
- Name: pixel-art
- Zweck: Pixel art w/ era palettes (NES, Game Boy, PICO-8).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Evalability unclear
- Priorität: low

### 205. popular-web-designs

- Pfad: `/home/piet/.hermes/skills/creative/popular-web-designs/SKILL.md`
- Name: popular-web-designs
- Zweck: 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 206. pretext

- Pfad: `/home/piet/.hermes/skills/creative/pretext/SKILL.md`
- Name: pretext
- Zweck: Use when building creative browser demos with @chenglou/pretext — DOM-free text layout for ASCII art, typographic flow around obstacles, text-as-geometry games, kinetic typography, and text-powered generative art. Produces single-file HTML
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 207. sketch

- Pfad: `/home/piet/.hermes/skills/creative/sketch/SKILL.md`
- Name: sketch
- Zweck: Throwaway HTML mockups: 2-3 design variants to compare.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 208. songwriting-and-ai-music

- Pfad: `/home/piet/.hermes/skills/creative/songwriting-and-ai-music/SKILL.md`
- Name: songwriting-and-ai-music
- Zweck: Songwriting craft and Suno AI music prompts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 209. touchdesigner-mcp

- Pfad: `/home/piet/.hermes/skills/creative/touchdesigner-mcp/SKILL.md`
- Name: touchdesigner-mcp
- Zweck: Control a running TouchDesigner instance via twozero MCP — create operators, set parameters, wire connections, execute Python, build real-time visuals. 36 native tools.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 210. jupyter-live-kernel

- Pfad: `/home/piet/.hermes/skills/data-science/jupyter-live-kernel/SKILL.md`
- Name: jupyter-live-kernel
- Zweck: Iterative Python via live Jupyter kernel (hamelnb).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing
- Priorität: high

### 211. autoresearch

- Pfad: `/home/piet/.hermes/skills/dev/autoresearch/SKILL.md`
- Name: autoresearch
- Zweck: Use when running a controlled Git/backup-based improvement loop for existing Hermes skills: inventory, rubric scoring, one-hypothesis edits, evals, append-only results, and optional read-only dashboard. Default mode is skills; MiniMax-M2.7-
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 212. defensive-hardening-verification

- Pfad: `/home/piet/.hermes/skills/devops/defensive-hardening-verification/SKILL.md`
- Name: defensive-hardening-verification
- Zweck: Class-level playbook for evidence-driven hardening of software boundaries: verify live assumptions, design defensive controls, run adversarial/negative tests, preserve rollback evidence, and report concise gate-ready outcomes. Covers Python
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 213. family-organizer-ui-polish-sprint

- Pfad: `/home/piet/.hermes/skills/devops/family-organizer-ui-polish-sprint/SKILL.md`
- Name: family-organizer-ui-polish-sprint
- Zweck: Execute a plan-driven UI polish sprint for the Family Organizer app. Covers worktree setup, token-harmonization, quick wins, pre-flight verification, commit, and Draft-PR creation. Targets the /admin area using design tokens from design/tok
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing
- Priorität: high

### 214. hermes-atlas-e2e-orchestrator

- Pfad: `/home/piet/.hermes/skills/devops/hermes-atlas-e2e-orchestrator/SKILL.md`
- Name: hermes-atlas-e2e-orchestrator
- Zweck: Hotpath for planning and governing Hermes→Atlas E2E sprints with explicit live-scope gates, local evidence first, dispatch/worker proof, receipts, and rollback discipline.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 215. hermes-context-budget-audits

- Pfad: `/home/piet/.hermes/skills/devops/hermes-context-budget-audits/SKILL.md`
- Name: hermes-context-budget-audits
- Zweck: Evidence-driven audits of Hermes prompt/context budget: distinguish initial-load content from profile/channel-gated tool schemas, measure file/skill/tool-schema sizes live, and produce operator-ready breakdown tables without turning analysi
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 216. hermes-dashboard-exposure

- Pfad: `/home/piet/.hermes/skills/devops/hermes-dashboard-exposure/SKILL.md`
- Name: hermes-dashboard-exposure
- Zweck: Safely plan and verify Hermes Dashboard mobile/Tailscale exposure and systemd persistence without breaking existing Funnel/Serve routes.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing; Workflow insufficiently structured
- Priorität: medium

### 217. hermes-dashboard-operations

- Pfad: `/home/piet/.hermes/skills/devops/hermes-dashboard-operations/SKILL.md`
- Name: hermes-dashboard-operations
- Zweck: Operate and expose the Hermes Agent web dashboard safely: local health checks, Kanban dashboard access, persistent user-service planning, Tailnet/mobile exposure, auth/security gates, and concise operator reporting without silently mutating
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 218. hermes-dispatcher-worker-runner

- Pfad: `/home/piet/.hermes/skills/devops/hermes-dispatcher-worker-runner/SKILL.md`
- Name: hermes-dispatcher-worker-runner
- Zweck: Safe operating protocol for Hermes Kanban dispatcher/worker-runtime tests and real worker runs; use before creating dispatchable tasks, promoting work to ready, retrying crashed workers, or interpreting worker logs.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 219. hermes-gateway-streaming-rca

- Pfad: `/home/piet/.hermes/skills/devops/hermes-gateway-streaming-rca/SKILL.md`
- Name: hermes-gateway-streaming-rca
- Zweck: Evidence-driven RCA workflow for Hermes Gateway streaming, platform overrides, provider call mode, and stale-call timeouts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 220. hermes-kanban-worker-scope-control

- Pfad: `/home/piet/.hermes/skills/devops/hermes-kanban-worker-scope-control/SKILL.md`
- Name: hermes-kanban-worker-scope-control
- Zweck: Mandatory scope-control protocol for Hermes Kanban worker tasks; use before any dispatched worker investigation, verifier review, or task design where forbidden systems/tools/paths matter.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 221. hermes-learning-skill-curator

- Pfad: `/home/piet/.hermes/skills/devops/hermes-learning-skill-curator/SKILL.md`
- Name: hermes-learning-skill-curator
- Zweck: Curate durable Hermes Kanban learnings into skills/templates/receipts without duplicating existing skills or promoting volatile runtime facts to memory.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 222. hermes-mode-router

- Pfad: `/home/piet/.hermes/skills/devops/hermes-mode-router/SKILL.md`
- Name: hermes-mode-router
- Zweck: First-touch routing playbook for the Hermes hub profile in Bot 1. Confidence-bands, deterministic @-mention mapping, dry-run-only output schema, termination rules. Used by hub to decide self_handle / coordinator / clarify / operator without
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 223. hermes-modelrouting-codex

- Pfad: `/home/piet/.hermes/skills/devops/hermes-modelrouting-codex/SKILL.md`
- Name: hermes-modelrouting-codex
- Zweck: Diagnose and harden Hermes profile model routing for Codex/MiniMax workers without leaking or copying credentials; use for default/admin/coder/dispatcher/planner routing audits and regression tests.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 224. hermes-planner-task-decomposition

- Pfad: `/home/piet/.hermes/skills/devops/hermes-planner-task-decomposition/SKILL.md`
- Name: hermes-planner-task-decomposition
- Zweck: Turn raw Hermes/Kanban goals into scoped Kanban-ready decompositions with acceptance criteria, dependencies, verifier gates, and scope contracts for Piet's default/admin/coder/dispatcher/planner roster.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 225. hermes-repo-sync

- Pfad: `/home/piet/.hermes/skills/devops/hermes-repo-sync/SKILL.md`
- Name: hermes-repo-sync
- Zweck: Hermes Agent local repo sync playbook: audit drift between local and upstream, choose merge or cherry-pick strategy, resolve conflicts, push cleanly, and prune deprecated branches. Use whenever Piet reports git drift, diverged branches, or
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 226. hermes-state-db-maintenance

- Pfad: `/home/piet/.hermes/skills/devops/hermes-state-db-maintenance/SKILL.md`
- Name: hermes-state-db-maintenance
- Zweck: Evidence-driven maintenance playbook for Hermes Agent state.db: size alerts, read-only retention/archive preflights, backup/downtime gates, FTS rebuild planning, VACUUM safety, and operator reporting without blind runtime mutation.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 227. hermes-toolset-allowlist-hardening

- Pfad: `/home/piet/.hermes/skills/devops/hermes-toolset-allowlist-hardening/SKILL.md`
- Name: hermes-toolset-allowlist-hardening
- Zweck: Harden Hermes Kanban per-task allowed_tools/MCP boundaries with dispatcher preflight, effective_toolsets evidence, and runtime-isolation caveat handling.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 228. hub-discord-msg

- Pfad: `/home/piet/.hermes/skills/devops/hub-discord-msg/SKILL.md`
- Name: hub-discord-msg
- Zweck: Use when Piet or Hub needs to send a Discord message to a channel or user via Hermes Gateway. Sends formatted messages with proper @mention pings to Coordinator or other Discord channels. Handles channel_id resolution and validates Discord
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 229. hub-pending-pickup

- Pfad: `/home/piet/.hermes/skills/devops/hub-pending-pickup/SKILL.md`
- Name: hub-pending-pickup
- Zweck: Use when Hub-LLM session starts or wakes up and should drain the ~/.hermes/hub_watcher_pending.jsonl file. Picks up pending hub-memory-warn, trigger, and Coord→Hub plan-requests that the watcher cron wrote but couldn't send to Discord direc
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 230. kanban-critic

- Pfad: `/home/piet/.hermes/skills/devops/kanban-critic/SKILL.md`
- Name: kanban-critic
- Zweck: Opt-in drift-catcher playbook for the Hermes critic profile. Three-state action (uphold / challenge / escalate_to_operator) on reviewer verdicts. Runs only when coordinator invokes it on high-risk, cross-system, or repeated-failure plans.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 231. kanban-execution-worker-readiness

- Pfad: `/home/piet/.hermes/skills/devops/kanban-execution-worker-readiness/SKILL.md`
- Name: kanban-execution-worker-readiness
- Zweck: Profile-specific readiness runbook for Piet's Kanban execution workers: admin, coder, reviewer, research, and critic. Use during worker self-audits, productive task preflight, and coordinator task design.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 232. kanban-orchestrator

- Pfad: `/home/piet/.hermes/skills/devops/kanban-orchestrator/SKILL.md`
- Name: kanban-orchestrator
- Zweck: Decomposition playbook and governance hotpath for Hermes Kanban orchestration: when to use the board, how to split tasks safely, scope contracts, worker routing, review gates, receipts, and no-dispatch boundaries.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 233. kanban-reviewer

- Pfad: `/home/piet/.hermes/skills/devops/kanban-reviewer/SKILL.md`
- Name: kanban-reviewer
- Zweck: Verdict-only review playbook for the Hermes reviewer profile. Three-state verdict (APPROVED / NEEDS_REVISION / BLOCKED) on plans + scope contracts + evidence refs. Hard rule: no APPROVED without verification-proof or explicit dry-run ration
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 234. kanban-worker

- Pfad: `/home/piet/.hermes/skills/devops/kanban-worker/SKILL.md`
- Name: kanban-worker
- Zweck: Hotpath for Hermes Kanban workers: task orientation, scope contracts, safe execution, receipts, dispatcher integration, and no-live-system guardrails.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 235. openclaw-bridge

- Pfad: `/home/piet/.hermes/skills/devops/openclaw-bridge/SKILL.md`
- Name: openclaw-bridge
- Zweck: Cross-system OpenClaw bridge skill for Coordinator-scoped Hermes→OpenClaw/Mission-Control handoffs with explicit live-scope approval, safety gates, capability envelopes, receipts, and rollback discipline.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 236. openclaw-discord-debug

- Pfad: `/home/piet/.hermes/skills/devops/openclaw-discord-debug/SKILL.md`
- Name: openclaw-discord-debug
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 237. openclaw-mc-hardening

- Pfad: `/home/piet/.hermes/skills/devops/openclaw-mc-hardening/SKILL.md`
- Name: openclaw-mc-hardening
- Zweck: Mission Control codebase hardening, R5-recovery gate implementation, and endpoint security hardening with strict read-only preflight, approval gates, tests, rollback, and receipts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 238. openclaw-model-catalog

- Pfad: `/home/piet/.hermes/skills/devops/openclaw-model-catalog/SKILL.md`
- Name: openclaw-model-catalog
- Zweck: Configure and troubleshoot OpenClaw's model catalog, provider definitions, and Discord model picker visibility. Covers why models appear or disappear in /model, the dual-location config requirement, auth-based filtering, and safe provider o
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 239. supabase-backup-operations

- Pfad: `/home/piet/.hermes/skills/devops/supabase-backup-operations/SKILL.md`
- Name: supabase-backup-operations
- Zweck: Operate Supabase/Postgres logical backups safely: pg_dump setup, secret-safe environment handling, manual verification, and cron activation gates.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing; Workflow insufficiently structured
- Priorität: medium

### 240. webhook-subscriptions

- Pfad: `/home/piet/.hermes/skills/devops/webhook-subscriptions/SKILL.md`
- Name: webhook-subscriptions
- Zweck: Webhook subscriptions: event-driven agent runs.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 241. dogfood

- Pfad: `/home/piet/.hermes/skills/dogfood/SKILL.md`
- Name: dogfood
- Zweck: Exploratory QA of web apps: find bugs, evidence, reports.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 242. himalaya

- Pfad: `/home/piet/.hermes/skills/email/himalaya/SKILL.md`
- Name: himalaya
- Zweck: Himalaya CLI: IMAP/SMTP email from terminal.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Workflow insufficiently structured
- Priorität: medium

### 243. grill-with-docs

- Pfad: `/home/piet/.hermes/skills/engineering/grill-with-docs/SKILL.md`
- Name: grill-with-docs
- Zweck: Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates documentation (CONTEXT.md, ADRs) inline as decisions crystallise. Use when user wants to stress-test a plan against their projec
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 244. minecraft-modpack-server

- Pfad: `/home/piet/.hermes/skills/gaming/minecraft-modpack-server/SKILL.md`
- Name: minecraft-modpack-server
- Zweck: Host modded Minecraft servers (CurseForge, Modrinth).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing
- Priorität: high

### 245. pokemon-player

- Pfad: `/home/piet/.hermes/skills/gaming/pokemon-player/SKILL.md`
- Name: pokemon-player
- Zweck: Play Pokemon via headless emulator + RAM reads.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 246. codebase-inspection

- Pfad: `/home/piet/.hermes/skills/github/codebase-inspection/SKILL.md`
- Name: codebase-inspection
- Zweck: Inspect codebases w/ pygount: LOC, languages, ratios.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: high

### 247. github-auth

- Pfad: `/home/piet/.hermes/skills/github/github-auth/SKILL.md`
- Name: github-auth
- Zweck: GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing
- Priorität: high

### 248. github-code-review

- Pfad: `/home/piet/.hermes/skills/github/github-code-review/SKILL.md`
- Name: github-code-review
- Zweck: Review PRs: diffs, inline comments via gh or REST.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 249. github-issues

- Pfad: `/home/piet/.hermes/skills/github/github-issues/SKILL.md`
- Name: github-issues
- Zweck: Create, triage, label, assign GitHub issues via gh or REST.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 250. github-pr-workflow

- Pfad: `/home/piet/.hermes/skills/github/github-pr-workflow/SKILL.md`
- Name: github-pr-workflow
- Zweck: GitHub PR lifecycle: branch, commit, open, CI, merge.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 251. github-repo-management

- Pfad: `/home/piet/.hermes/skills/github/github-repo-management/SKILL.md`
- Name: github-repo-management
- Zweck: Clone/create/fork repos; manage remotes, releases.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 252. kanban-review-lane-classification

- Pfad: `/home/piet/.hermes/skills/hermes-kanban/kanban-review-lane-classification/SKILL.md`
- Name: kanban-review-lane-classification
- Zweck: >
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 253. native-mcp

- Pfad: `/home/piet/.hermes/skills/mcp/native-mcp/SKILL.md`
- Name: native-mcp
- Zweck: MCP client: connect servers, register tools (stdio/HTTP).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 254. gif-search

- Pfad: `/home/piet/.hermes/skills/media/gif-search/SKILL.md`
- Name: gif-search
- Zweck: Search/download GIFs from Tenor via curl + jq.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: low

### 255. heartmula

- Pfad: `/home/piet/.hermes/skills/media/heartmula/SKILL.md`
- Name: heartmula
- Zweck: HeartMuLa: Suno-like song generation from lyrics + tags.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 256. songsee

- Pfad: `/home/piet/.hermes/skills/media/songsee/SKILL.md`
- Name: songsee
- Zweck: Audio spectrograms/features (mel, chroma, MFCC) via CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Examples missing; Workflow insufficiently structured
- Priorität: high

### 257. spotify

- Pfad: `/home/piet/.hermes/skills/media/spotify/SKILL.md`
- Name: spotify
- Zweck: Spotify: play, search, queue, manage playlists and devices.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured; Evalability unclear
- Priorität: low

### 258. youtube-content

- Pfad: `/home/piet/.hermes/skills/media/youtube-content/SKILL.md`
- Name: youtube-content
- Zweck: YouTube transcripts to summaries, threads, blogs.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 259. evaluating-llms-harness

- Pfad: `/home/piet/.hermes/skills/mlops/evaluation/lm-evaluation-harness/SKILL.md`
- Name: evaluating-llms-harness
- Zweck: lm-eval-harness: benchmark LLMs (MMLU, GSM8K, etc.).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 260. weights-and-biases

- Pfad: `/home/piet/.hermes/skills/mlops/evaluation/weights-and-biases/SKILL.md`
- Name: weights-and-biases
- Zweck: W&B: log ML experiments, sweeps, model registry, dashboards.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 261. huggingface-hub

- Pfad: `/home/piet/.hermes/skills/mlops/huggingface-hub/SKILL.md`
- Name: huggingface-hub
- Zweck: HuggingFace hf CLI: search/download/upload models, datasets.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 262. llama-cpp

- Pfad: `/home/piet/.hermes/skills/mlops/inference/llama-cpp/SKILL.md`
- Name: llama-cpp
- Zweck: llama.cpp local GGUF inference + HF Hub model discovery.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 263. obliteratus

- Pfad: `/home/piet/.hermes/skills/mlops/inference/obliteratus/SKILL.md`
- Name: obliteratus
- Zweck: OBLITERATUS: abliterate LLM refusals (diff-in-means).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 264. outlines

- Pfad: `/home/piet/.hermes/skills/mlops/inference/outlines/SKILL.md`
- Name: outlines
- Zweck: Outlines: structured JSON/regex/Pydantic LLM generation.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 265. serving-llms-vllm

- Pfad: `/home/piet/.hermes/skills/mlops/inference/vllm/SKILL.md`
- Name: serving-llms-vllm
- Zweck: vLLM: high-throughput LLM serving, OpenAI API, quantization.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 266. audiocraft-audio-generation

- Pfad: `/home/piet/.hermes/skills/mlops/models/audiocraft/SKILL.md`
- Name: audiocraft-audio-generation
- Zweck: AudioCraft: MusicGen text-to-music, AudioGen text-to-sound.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 267. segment-anything-model

- Pfad: `/home/piet/.hermes/skills/mlops/models/segment-anything/SKILL.md`
- Name: segment-anything-model
- Zweck: SAM: zero-shot image segmentation via points, boxes, masks.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit
- Priorität: high

### 268. dspy

- Pfad: `/home/piet/.hermes/skills/mlops/research/dspy/SKILL.md`
- Name: dspy
- Zweck: DSPy: declarative LM programs, auto-optimize prompts, RAG.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 269. axolotl

- Pfad: `/home/piet/.hermes/skills/mlops/training/axolotl/SKILL.md`
- Name: axolotl
- Zweck: Axolotl: YAML LLM fine-tuning (LoRA, DPO, GRPO).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 270. fine-tuning-with-trl

- Pfad: `/home/piet/.hermes/skills/mlops/training/trl-fine-tuning/SKILL.md`
- Name: fine-tuning-with-trl
- Zweck: TRL: SFT, DPO, PPO, GRPO, reward modeling for LLM RLHF.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 271. unsloth

- Pfad: `/home/piet/.hermes/skills/mlops/training/unsloth/SKILL.md`
- Name: unsloth
- Zweck: Unsloth: 2-5x faster LoRA/QLoRA fine-tuning, less VRAM.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 272. obsidian

- Pfad: `/home/piet/.hermes/skills/note-taking/obsidian/SKILL.md`
- Name: obsidian
- Zweck: Read, search, create, and edit notes in the Obsidian vault.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 273. openclaw-model-catalog-update

- Pfad: `/home/piet/.hermes/skills/openclaw/openclaw-model-catalog-update/SKILL.md`
- Name: openclaw-model-catalog-update
- Zweck: Schritt-für-Schritt-Anleitung zum Hinzufügen neuer Modelle zu OpenClaw über OpenRouter (oder andere Provider). Covers ID-Recherche, Preis-Ermittlung, Config-Patch an zwei Stellen, und Validierung.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 274. airtable

- Pfad: `/home/piet/.hermes/skills/productivity/airtable/SKILL.md`
- Name: airtable
- Zweck: Airtable REST API via curl. Records CRUD, filters, upserts.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 275. family-organizer-ui-polish

- Pfad: `/home/piet/.hermes/skills/productivity/family-organizer-ui-polish/SKILL.md`
- Name: family-organizer-ui-polish
- Zweck: Kompletter UI/UX Polish Workflow für Family Organizer — Audit → Plan → Implementierung → Review → PR
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 276. google-workspace

- Pfad: `/home/piet/.hermes/skills/productivity/google-workspace/SKILL.md`
- Name: google-workspace
- Zweck: Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 277. linear

- Pfad: `/home/piet/.hermes/skills/productivity/linear/SKILL.md`
- Name: linear
- Zweck: Linear: manage issues, projects, teams via GraphQL + curl.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 278. maps

- Pfad: `/home/piet/.hermes/skills/productivity/maps/SKILL.md`
- Name: maps
- Zweck: Geocode, POIs, routes, timezones via OpenStreetMap/OSRM.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 279. nano-pdf

- Pfad: `/home/piet/.hermes/skills/productivity/nano-pdf/SKILL.md`
- Name: nano-pdf
- Zweck: Edit PDF text/typos/titles via nano-pdf CLI (NL prompts).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 280. notion

- Pfad: `/home/piet/.hermes/skills/productivity/notion/SKILL.md`
- Name: notion
- Zweck: Notion API + ntn CLI: pages, databases, markdown, Workers.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 281. ocr-and-documents

- Pfad: `/home/piet/.hermes/skills/productivity/ocr-and-documents/SKILL.md`
- Name: ocr-and-documents
- Zweck: Extract text from PDFs/scans (pymupdf, marker-pdf).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 282. powerpoint

- Pfad: `/home/piet/.hermes/skills/productivity/powerpoint/SKILL.md`
- Name: powerpoint
- Zweck: Create, read, edit .pptx decks, slides, notes, templates.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 283. teams-meeting-pipeline

- Pfad: `/home/piet/.hermes/skills/productivity/teams-meeting-pipeline/SKILL.md`
- Name: teams-meeting-pipeline
- Zweck: Operate the Teams meeting summary pipeline via Hermes CLI — summarize meetings, inspect pipeline status, replay jobs, manage Microsoft Graph subscriptions.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 284. godmode

- Pfad: `/home/piet/.hermes/skills/red-teaming/godmode/SKILL.md`
- Name: godmode
- Zweck: Jailbreak LLMs: Parseltongue, GODMODE, ULTRAPLINIAN.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 285. arxiv

- Pfad: `/home/piet/.hermes/skills/research/arxiv/SKILL.md`
- Name: arxiv
- Zweck: Search arXiv papers by keyword, author, category, or ID.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit
- Priorität: high

### 286. blogwatcher

- Pfad: `/home/piet/.hermes/skills/research/blogwatcher/SKILL.md`
- Name: blogwatcher
- Zweck: Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing; Safety rules missing or not explicit; Workflow insufficiently structured
- Priorität: high

### 287. free-model-audit

- Pfad: `/home/piet/.hermes/skills/research/free-model-audit/SKILL.md`
- Name: free-model-audit
- Zweck: Use when asked to audit code, vault, or taskboard state using free OpenRouter models (Owl Alpha, Qwen3 Coder, Ring, etc.). Run read-only scans, collect findings as JSONL, and generate markdown summaries. Never use free models for dispatch,
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 288. llm-wiki

- Pfad: `/home/piet/.hermes/skills/research/llm-wiki/SKILL.md`
- Name: llm-wiki
- Zweck: Karpathy's LLM Wiki: build/query interlinked markdown KB.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 289. polymarket

- Pfad: `/home/piet/.hermes/skills/research/polymarket/SKILL.md`
- Name: polymarket
- Zweck: Query Polymarket: markets, prices, orderbooks, history.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Evalability unclear
- Priorität: high

### 290. research-paper-writing

- Pfad: `/home/piet/.hermes/skills/research/research-paper-writing/SKILL.md`
- Name: research-paper-writing
- Zweck: Write ML papers for NeurIPS/ICML/ICLR: design→submit.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 291. openhue

- Pfad: `/home/piet/.hermes/skills/smart-home/openhue/SKILL.md`
- Name: openhue
- Zweck: Control Philips Hue lights, scenes, rooms via OpenHue CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: nein
- Schwächen: Safety rules missing or not explicit; Output contract missing; Examples missing; Workflow insufficiently structured
- Priorität: high

### 292. xurl

- Pfad: `/home/piet/.hermes/skills/social-media/xurl/SKILL.md`
- Name: xurl
- Zweck: X/Twitter via xurl CLI: post, search, DM, media, v2 API.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 293. debugging-hermes-tui-commands

- Pfad: `/home/piet/.hermes/skills/software-development/debugging-hermes-tui-commands/SKILL.md`
- Name: debugging-hermes-tui-commands
- Zweck: Debug Hermes TUI slash commands: Python, gateway, Ink UI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: nein
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Safety rules missing or not explicit; Output contract missing
- Priorität: high

### 294. grill-me

- Pfad: `/home/piet/.hermes/skills/software-development/grill-me/SKILL.md`
- Name: grill-me
- Zweck: Use when the user wants a hard, constructive critique of a plan, idea, design, implementation, argument, or decision before acting.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 295. hermes-agent-skill-authoring

- Pfad: `/home/piet/.hermes/skills/software-development/hermes-agent-skill-authoring/SKILL.md`
- Name: hermes-agent-skill-authoring
- Zweck: Author in-repo SKILL.md: frontmatter, validator, structure.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 296. hermes-s6-container-supervision

- Pfad: `/home/piet/.hermes/skills/software-development/hermes-s6-container-supervision/SKILL.md`
- Name: hermes-s6-container-supervision
- Zweck: Modify, debug, or extend the s6-overlay supervision tree inside the Hermes Agent Docker image — adding new services, debugging profile gateways, understanding the Architecture B main-program pattern.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing; Workflow insufficiently structured
- Priorität: low

### 297. karpathy-coding-guardrails

- Pfad: `/home/piet/.hermes/skills/software-development/karpathy-coding-guardrails/SKILL.md`
- Name: karpathy-coding-guardrails
- Zweck: Karpathy-inspired guardrails for cautious, simple, surgical, verifiable coding changes.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 298. node-inspect-debugger

- Pfad: `/home/piet/.hermes/skills/software-development/node-inspect-debugger/SKILL.md`
- Name: node-inspect-debugger
- Zweck: Debug Node.js via --inspect + Chrome DevTools Protocol CLI.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 299. plan

- Pfad: `/home/piet/.hermes/skills/software-development/plan/SKILL.md`
- Name: plan
- Zweck: Plan mode: write markdown plan to .hermes/plans/, no exec.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Activation criteria unclear/missing; Examples missing
- Priorität: medium

### 300. python-debugpy

- Pfad: `/home/piet/.hermes/skills/software-development/python-debugpy/SKILL.md`
- Name: python-debugpy
- Zweck: Debug Python: pdb REPL + debugpy remote (DAP).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: nein
- Beispiele vorhanden: ja
- Schwächen: Output contract missing; Workflow insufficiently structured
- Priorität: high

### 301. requesting-code-review

- Pfad: `/home/piet/.hermes/skills/software-development/requesting-code-review/SKILL.md`
- Name: requesting-code-review
- Zweck: Pre-commit review: security scan, quality gates, auto-fix.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 302. spike

- Pfad: `/home/piet/.hermes/skills/software-development/spike/SKILL.md`
- Name: spike
- Zweck: Throwaway experiments to validate an idea before build.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: nein
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Activation criteria unclear/missing
- Priorität: medium

### 303. subagent-driven-development

- Pfad: `/home/piet/.hermes/skills/software-development/subagent-driven-development/SKILL.md`
- Name: subagent-driven-development
- Zweck: Execute plans via delegate_task subagents (2-stage review).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 304. systematic-debugging

- Pfad: `/home/piet/.hermes/skills/software-development/systematic-debugging/SKILL.md`
- Name: systematic-debugging
- Zweck: 4-phase root cause debugging: understand bugs before fixing.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 305. test-driven-development

- Pfad: `/home/piet/.hermes/skills/software-development/test-driven-development/SKILL.md`
- Name: test-driven-development
- Zweck: TDD: enforce RED-GREEN-REFACTOR, tests before code.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: nein
- Schwächen: Examples missing
- Priorität: low

### 306. use-claude-cli

- Pfad: `/home/piet/.hermes/skills/software-development/use-claude-cli/SKILL.md`
- Name: use-claude-cli
- Zweck: Spawn the Claude Code CLI headless via the operator's Max-plan subscription for one-shot, supervised tasks (file edits, vault synthesis, code refactors, doc critique).
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Workflow insufficiently structured
- Priorität: low

### 307. writing-plans

- Pfad: `/home/piet/.hermes/skills/software-development/writing-plans/SKILL.md`
- Name: writing-plans
- Zweck: Write implementation plans: bite-sized tasks, paths, code.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: No obvious structural deficits by heuristic.
- Priorität: low

### 308. yuanbao

- Pfad: `/home/piet/.hermes/skills/yuanbao/SKILL.md`
- Name: yuanbao
- Zweck: Yuanbao (元宝) groups: @mention users, query info/members.
- Frontmatter vorhanden: ja
- Aktivierungskriterien vorhanden: ja
- Safety-Regeln vorhanden: ja
- Output-Vertrag vorhanden: ja
- Beispiele vorhanden: ja
- Schwächen: Evalability unclear
- Priorität: low
