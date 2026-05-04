# Changelog

All notable changes to **DataFlow Finance** are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Planned
- Dashboard web (React + Recharts)
- Integração Omie / Conta Azul
- Autenticação multi-tenant completa (múltiplos clientes isolados)
- Pipeline schedulado (cron / Celery Beat)

---

## [0.5.0] — 2026-05-04

> Documentação expandida, DevEx e CORS configurável.

### Added
- `CONTRIBUTING.md` com guia de setup, workflow de desenvolvimento, padrões de commit e calendário de melhorias automáticas
- Campo `allowed_origins` em `config.py` (pydantic-settings) para configurar origens CORS via variável de ambiente `DATAFLOW_ALLOWED_ORIGINS`

### Changed
- `main.py`: `CORSMiddleware` agora usa `settings.allowed_origins` em vez de `["*"]` hardcoded — CORS agora é configurável por ambiente sem tocar no código
- `.env.example`: removida variável fantasma `DATAFLOW_API_KEY` (nunca existiu em `config.py`); `DATAFLOW_ALLOWED_ORIGINS` agora documentada com formato correto (JSON array)

### Fixed
- Inconsistência entre `.env.example` e `config.py` onde `DATAFLOW_API_KEY` era referenciada mas nunca lida pela aplicação

---

## [0.4.0] — 2026-04-24 / 2026-04-26

> Documentação expandida, melhoras de design e DevEx.

### Added
- `requirements-dev.txt` com dependências de desenvolvimento (pytest, ruff, mypy, pandas-stubs)
- `Makefile` com comandos padrão: `make run`, `make test`, `make lint`, `make type-check`

### Changed
- README expandido com exemplos de `curl` para todos os endpoints (leads, alertas, métricas, ETL)
- `.env.example` corrigido e completamente documentado

### Design
- Texto hero com gradiente CSS animado
- Efeito glow nos cards ao hover
- Pulsação no plano "Profissional" da seção de preços

---

## [0.3.0] — 2026-04-22 / 2026-04-23

> Segurança de API, paginação e métricas reais.

### Added
- Autenticação por API key (`X-API-Key` header) no `GET /api/v1/leads/`
- Paginação (`limit` / `offset`) em `GET /api/v1/leads/`
- Métrica real `alerts_open` calculada em `GET /api/v1/metrics/dashboard`

### Changed
- Copy da hero section revisado para maior conversão
- FAQ: nova entrada com objeção de preço respondida
- Seção de depoimentos: badge de verificação adicionado ao testimonial principal

---

## [0.2.0] — 2026-04-19 / 2026-04-21

> Camada de UX imersiva, acessibilidade e SEO.

### Added — Acessibilidade (a11y)
- ARIA labels em todos os elementos interativos
- Skip-navigation link para leitores de tela
- Controles de formulário acessíveis com `aria-describedby` e `role`
- Focus rings visíveis em toda a navegação por teclado

### Added — SEO & Performance
- JSON-LD structured data (schema.org `SoftwareApplication`)
- Canonical URL e resource hints (`preconnect`, `dns-prefetch`)
- SVG favicon e meta tags Open Graph / Twitter Card

### Added — Frontend
- Navbar responsiva com menu hamburger e drawer Alpine.js
- Barra de progresso de scroll, botão scroll-to-top e container de toast
- Animações scroll-reveal e contadores de números animados
- Footer com 4 colunas, links sociais e widget de status da API
- Prova social, barra de confiança e depoimentos de clientes
- Countdown de urgência e CTA sticky no rodapé mobile
- Efeito de partículas no hero, typewriter effect, mouse tilt e confetti
- Exit-intent modal e banner de consentimento LGPD
- Mockup do dashboard financeiro animado
- Mouse spotlight, contador de visitantes ao vivo e glitch effect no logo
- Orb ambiente animado, sparkline crescente e count-up de KPIs
- Dots de flow animados no diagrama de pipeline
- Marquee infinito de logos de parceiros/integrações
- Compartilhamento do resultado do ROI via WhatsApp
- Anel de progresso de scroll e bolha flutuante do WhatsApp

### Fixed
- Erro de assertion do FastAPI ao retornar resposta 204 sem corpo no endpoint `DELETE /alerts/subscribers/{id}`

---

## [0.1.0] — 2026-04-16

> Lançamento inicial — ETL core, API e landing page.

### Added — Backend
- Motor ETL completo: **Extract → Validate → Transform → Load**
- `IfoodExtractor` — lê CSV de pedidos iFood e normaliza colunas
- `PDVExtractor` — lê CSV/XLSX do PDV com suporte a múltiplos layouts
- `AcquirerExtractor` — lê extrato Stone/Cielo e identifica transações
- `FinancialTransformer` — merge DRE + detecção de 4 tipos de furo de caixa
- `ReportLoader` — relatório Excel com 4 abas (DRE, ledger, furos, quarentena) + JSON
- `POST /api/v1/run-etl` — endpoint principal do pipeline
- `GET /api/v1/metrics/dashboard` — KPIs consolidados dos últimos 6 meses
- `GET /api/v1/metrics/period/{month}` — métricas de um mês específico
- `POST /api/v1/alerts/subscribers` — registro de webhooks para alertas
- `GET /api/v1/alerts/active` — alertas abertos (não reconhecidos)
- `POST /api/v1/alerts/{id}/ack` — reconhecimento de alerta
- `POST /api/v1/leads/` — captura de lead do formulário da landing page
- `GET /api/v1/leads/` — listagem de leads (protegida por API key)
- Logging estruturado com Loguru (stdout colorido + arquivo rotativo 7 dias / 50 MB)
- `GET /health` — liveness probe para load balancers
- `config.py` (pydantic-settings) com todas as variáveis configuráveis via `.env`

### Added — Frontend
- Landing page B2B dark corporate (navy-950 + emerald-500, fonte Inter)
- Seções: hero, cases, Quem Somos, calculadora de ROI, planos, FAQ, rodapé
- Calculadora de ROI interativa com Alpine.js
- Formulário de captura de leads integrado ao backend
- Galeria de integrações (iFood, Rappi, Stone, Cielo, Getnet, Rede)

### Added — Infraestrutura
- `README.md` com documentação de arquitetura, setup e uso da API
- `.env.example` com todas as variáveis comentadas
- `.gitignore` cobrindo Python, venvs, `.env` e outputs
