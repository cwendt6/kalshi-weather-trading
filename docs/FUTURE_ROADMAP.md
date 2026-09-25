# Future Roadmap & Ideas

## Base Ecosystem Integration (Long-term Goal)

**Owner:** Cole
**Date Added:** January 26, 2026

### Vision
Integrate the Kalshi prediction market trading system with the Base (Coinbase L2) crypto ecosystem. The goal is to release a decentralized app (dApp) on Base that incorporates prediction market trading capabilities.

### Potential Approaches

#### Option 1: Prediction Market on Base
Build a native prediction market protocol on Base that:
- Uses the forecasting/edge calculation algorithms from this project
- Leverages Base's low fees and fast transactions
- Integrates with existing DeFi protocols for liquidity

#### Option 2: Bridge/Aggregator
Create an app that:
- Aggregates prediction markets across Kalshi, Polymarket, and on-chain markets
- Provides unified analytics and forecasting
- Enables cross-platform arbitrage detection
- Uses Base for settlement/payments

#### Option 3: AI Trading Agent on Base
Deploy an autonomous trading agent that:
- Runs on-chain with transparent decision making
- Uses the LLM forecasting from this project (off-chain oracle)
- Manages a treasury and distributes profits to token holders
- Similar to AI agent tokens but for prediction markets

#### Option 4: $TOSHI Integration
Leverage existing $TOSHI memecoin holdings to bootstrap the prediction market:
- **Token Pool**: Use $TOSHI as the native token for prediction market liquidity
- **Staking Rewards**: Stake $TOSHI to earn a share of trading profits
- **Governance**: $TOSHI holders vote on new market categories/features
- **Prediction Collateral**: Use $TOSHI as collateral for prediction positions
- **Yield Generation**: Deploy prediction market profits back into $TOSHI ecosystem

**Benefits of $TOSHI approach:**
- Already have a position (no need to bootstrap liquidity from scratch)
- Established community on Base
- Memecoin momentum can drive adoption
- Lower barrier than creating new token

### Technical Considerations

**Base-Specific:**
- EVM-compatible (Solidity/Vyper)
- Low gas fees (~$0.01 per transaction)
- Coinbase ecosystem integration (Smart Wallet, Verifications)
- [Base documentation](https://docs.base.org/)

**Existing Projects to Research:**
- Azuro Protocol (on-chain prediction markets)
- Zeitgeist (Polkadot prediction market)
- Gnosis/Omen (Ethereum prediction markets)
- Thales Market (Optimism)

**Smart Contract Needs:**
- Market creation and resolution
- AMM or order book for liquidity
- Oracle integration for outcome resolution
- Treasury management

### Timeline
1. **Phase 1 (Current):** Build and validate Kalshi trading system
2. **Phase 2 (3-6 months):** Achieve profitability, refine algorithms
3. **Phase 3 (6-12 months):** Research Base ecosystem, design dApp architecture
4. **Phase 4 (12+ months):** Build and deploy on Base

### Notes
- Consider launching on Base testnet first for validation
- Look into Base's ecosystem grants program
- Explore partnerships with existing prediction market projects
- Keep tracking Polymarket's potential US launch (may affect strategy)

---

## Other Future Ideas

### Polymarket Integration (When US Access Available)
- Cross-platform arbitrage between Kalshi and Polymarket
- Unified portfolio management
- Use Polymarket's deeper liquidity for certain markets

### Social Signal Integration (US-D03)
**last30days-skill Integration**
- GitHub: https://github.com/mvanhorn/last30days-skill
- Claude Code skill that researches trending topics across Reddit/X
- Uses engagement metrics (upvotes, likes) as community validation signals
- Could detect emerging consensus before it's priced into prediction markets

**Requirements:**
- OpenAI API key (for Reddit search)
- xAI API key (for X/Twitter search)
- 24-hour cache TTL reduces repeated calls

**Cost Concerns:**
- Documented case of "$200 overnight bill" from runaway API calls
- Need usage caps and rate limiting before deployment
- Recommend implementing only after system is profitable

**Implementation Notes:**
- Install to `~/.claude/skills/last30days`
- Trigger with "[topic] for predictions" or "what's happening with [topic]"
- Use sparingly for high-value market research only

---

### Bankr/Clanker Integration (US-D04)
**AI Agent Token Infrastructure on Base**

**Clanker - Token Deployment Engine:**
- AI agent that auto-deploys tokens based on prompts
- 355K+ tokens deployed, $34.4M in fees generated
- Ecosystem market cap: $172M+
- Creates Uniswap V3 liquidity pools automatically
- GitHub/Docs: Research needed

**Bankr - DeFAI Terminal:**
- Website: https://bankr.bot/
- AI-powered crypto assistant for token operations
- Supports Base, Ethereum, Polygon, Solana
- Can launch tokens via X (Twitter), Farcaster, XMTP
- BNKR token market cap: ~$20M

**Potential Use Cases for Prediction Markets:**
1. **Automated Pool Creation**: Use Clanker to deploy prediction market tokens
2. **Social Trading**: Bankr bot for prediction market interactions on Farcaster/X
3. **$TOSHI Integration**: Connect existing holdings to Clanker ecosystem
4. **AI Agent Markets**: Create markets about AI agent performance

**Technical Notes:**
- 40% fee distribution mechanism in Clanker
- Uniswap V3 ETH trading pairs
- ENS integration (e.g., clawd.atg.eth pattern)

**Related Projects:**
- Clawdbot (https://clawd.bot/) - Open-source AI assistant with token
- CLAWD token: ~$4M market cap, 8.9K holders

---

### Additional Data Sources
- Twitter/X API (when budget allows)
- Discord monitoring for crypto markets
- Telegram signals
- On-chain data for crypto-related predictions

### Advanced Strategies
- Market making on Kalshi
- Multi-leg trades (related markets)
- Event-driven trading around scheduled releases
- Weather market specialization (proven edge per research)

---

### LLM Forecasting (When Profitable)
- Add Claude/GPT integration after rule-based system proves edge
- Track API costs vs. additional alpha generated
- Consider fine-tuning smaller models on Kalshi-specific data
- Interface already designed in forecaster.py for easy swap-in

#### Kimi K2.5 - Recommended Open-Source Option (US-D01)
**Researched: January 27, 2026**

Kimi K2.5 by Moonshot AI is a strong candidate for LLM-based probability forecasting:

**Key Specs:**
- 1 trillion parameters (32B active via MoE architecture)
- 256K context window
- Open weights on HuggingFace (Modified MIT License)
- "Agent Swarm" capability - up to 100 sub-agents for research tasks

**Cost Comparison (per 1M tokens):**
| Model | Input | Output |
|-------|-------|--------|
| Kimi K2 | $0.60 | $2.50 |
| Claude Sonnet 4 | ~$3.00 | ~$15.00 |
| Kimi K2 Turbo | $1.15 | $8.00 |

**~10x cheaper than Claude** for similar reasoning tasks.

**Benchmarks vs Claude/GPT:**
- SWE-Bench: 71-77% (Claude Opus: 77-81%)
- BrowseComp (web research): 60.2% (Claude: 24.1%) - **excellent for research**
- Competitive on math/science reasoning

**Access Methods:**
1. **API**: platform.moonshot.ai (cheapest option for testing)
2. **Self-hosted**: Requires 8x H200 GPUs (~$50k hardware) or 2x GPUs for 4-bit quantized
3. **NVIDIA NIM**: Cloud deployment option

**Calibration Concerns (applies to ALL LLMs):**
- Research shows LLM probability estimates have ECE 0.12-0.40 vs human superforecasters 0.03-0.05
- Overconfidence is universal - when LLMs say 90%, expect 20-30% error
- Recommendation: Apply Platt scaling or temperature calibration before using for trading

**Implementation Plan:**
1. Start with API for low-cost testing
2. Build calibration layer on raw outputs
3. Backtest extensively on historical Kalshi markets
4. Consider ensemble with rule-based system (research shows 12-model ensemble rivals human crowds)
5. If successful, consider fine-tuning open weights on Kalshi-specific data

**Links:**
- HuggingFace: huggingface.co/moonshotai/Kimi-K2.5
- API: platform.moonshot.ai
- Blog: kimi.com/blog/kimi-k2-5.html

---

*Last Updated: January 27, 2026*
