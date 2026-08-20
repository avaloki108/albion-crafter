.
├── .gitignore
├── README.md
├── docs
│   ├── CRAFTING_PROFILE.md
│   ├── DATABASE.md
│   ├── DATA_SOURCES.md
│   ├── FIND_ME_MONEY.md
│   ├── MECHANICS.md
│   └── OPPORTUNITY_ENGINE.md
├── pyproject.toml
├── scripts
│   ├── profile_planner_workloads.py
│   ├── verify_multicapacity_optimizer.py
│   └── verify_planning_preprocessing.py
├── src
│   └── albion_crafter
│       ├── __init__.py
│       ├── core
│       │   ├── __init__.py
│       │   ├── actionability.py
│       │   ├── arbitrage.py
│       │   ├── calculator.py
│       │   ├── city_bonuses.py
│       │   ├── crafting_profile.py
│       │   ├── fees.py
│       │   ├── focus.py
│       │   ├── freshness.py
│       │   ├── mechanics.py
│       │   ├── models.py
│       │   ├── provenance.py
│       │   ├── refining_bonuses.py
│       │   ├── returns.py
│       │   └── stations.py
│       ├── data
│       │   ├── __init__.py
│       │   ├── cities.py
│       │   ├── items.py
│       │   ├── recipes.py
│       │   ├── sample_data.py
│       │   └── static_importer.py
│       ├── database
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── database.py
│       │   ├── v3.py
│       │   └── v4.py
│       ├── main.py
│       ├── market
│       │   ├── __init__.py
│       │   ├── aodp.py
│       │   ├── cache.py
│       │   ├── history.py
│       │   ├── history_cache.py
│       │   ├── liquidity.py
│       │   ├── models.py
│       │   ├── pricing.py
│       │   └── recipe_refresh.py
│       ├── opportunity
│       │   ├── __init__.py
│       │   ├── filtering.py
│       │   ├── models.py
│       │   ├── pricing.py
│       │   ├── scanner.py
│       │   └── service.py
│       ├── planning
│       │   ├── __init__.py
│       │   ├── arbitrage.py
│       │   ├── candidates.py
│       │   ├── current_refresh.py
│       │   ├── explanations.py
│       │   ├── export.py
│       │   ├── models.py
│       │   ├── multicapacity.py
│       │   ├── optimizer.py
│       │   ├── preflight.py
│       │   ├── quantity.py
│       │   ├── routes.py
│       │   ├── service.py
│       │   ├── validation.py
│       │   └── workload.py
│       ├── ui
│       │   ├── __init__.py
│       │   ├── calculator_refresh_worker.py
│       │   ├── calculator_view.py
│       │   ├── common.py
│       │   ├── craft_scanner.py
│       │   ├── find_money.py
│       │   ├── find_money_worker.py
│       │   ├── main_window.py
│       │   ├── market_data.py
│       │   ├── scan_worker.py
│       │   └── settings_view.py
│       └── update_static_data.py
├── tests
│   ├── fixtures
│   │   └── refining-upstream-contract
│   │       ├── PROVENANCE.md
│   │       ├── formatted-items.json
│   │       └── items.json
│   ├── test_arbitrage_v06.py
│   ├── test_calculator.py
│   ├── test_calculator_ui.py
│   ├── test_core_v4.py
│   ├── test_current_refresh.py
│   ├── test_database.py
│   ├── test_database_v3.py
│   ├── test_database_v4.py
│   ├── test_domain_v3.py
│   ├── test_fees.py
│   ├── test_find_money_ui.py
│   ├── test_history.py
│   ├── test_liquidity.py
│   ├── test_market.py
│   ├── test_market_data_ui.py
│   ├── test_market_v04.py
│   ├── test_main_window_calculator.py
│   ├── test_mechanics.py
│   ├── test_opportunity_filtering.py
│   ├── test_opportunity_performance.py
│   ├── test_opportunity_pricing.py
│   ├── test_opportunity_scanner.py
│   ├── test_opportunity_service.py
│   ├── test_plan_export.py
│   ├── test_planning_candidates.py
│   ├── test_planning_models.py
│   ├── test_planning_optimizer.py
│   ├── test_planning_preflight.py
│   ├── test_planning_service.py
│   ├── test_planning_validation.py
│   ├── test_planning_workload.py
│   ├── test_pricing.py
│   ├── test_recipe_price_refresh.py
│   ├── test_refining_v05.py
│   ├── test_returns.py
│   ├── test_static_importer.py
│   └── test_ui_profile_v3.py
├── tree.md
└── uv.lock

15 directories, 124 files
