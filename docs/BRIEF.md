
Hackathon Brief: Building an OR Agent – From Business Question to Optimal Decision
1. Introduction & Context
Wikipedia:

Operations research (OR) encompasses the development and the use of a wide range of problem-solving techniques and methods applied in the pursuit of improved decision-making and efficiency, such as simulation, mathematical optimization, queueing theory and other stochastic-process models, Markov decision processes, econometric methods, data envelopment analysis, ordinal priority approach, neural networks, expert systems, decision analysis, and the analytic hierarchy process. Nearly all of these techniques involve the construction of mathematical models that attempt to describe the system. Because of the computational and statistical nature of most of these fields, OR also has strong ties to computer science and analytics. Operational researchers faced with a new problem must determine which of these techniques are most appropriate given the nature of the system, the goals for improvement, and constraints on time and computing power, or develop a new technique specific to the problem at hand (and, afterwards, to that type of problem). [https://en.wikipedia.org/wiki/Operations_research]

Many critical business decisions require complex optimization across multiple variables, constraints, and conflicting objectives. While Operations Research (OR) provides incredibly powerful deterministic methods to solve these problems, applying these techniques typically requires highly specialized expertise and technical modeling skills.

Business users often struggle to bridge the gap: translating real-world, natural-language operational questions into structured, rigid mathematical optimization models that can be solved computationally.

The financial industry faces numerous types of problems where OR techniques may yield the optimal solution, e.g.,

resource allocation
assignment problems
scheduling / staffing
pricing science
These areas should give you some idea, but in this challenge you are encouraged to bring your own problem statement and data, or pick one of the examples from below to get started.

Please note that it is not the goal to solve a specific problem, but to build an AI Agent that can solve new problems independently. But more on this in the following chapter.

2. Objective & The Core Challenge
Your objective in this hackathon is to develop an AI-powered Operations Research (OR) agent that dismantles this technical bottleneck. Your agent must act as an end-to-end pipeline capable of operating independently for a non-technical business stakeholder.

The Agent's Core Workflow: Ingest a natural-language business problem -> Analyze the accompanying tabular dataset -> Formulate an appropriate mathematical optimization approach -> Dynamically code and solve the problem using OR libraries (e.g., Google OR-Tools, Gurobi, SciPy) ->D Translate the raw mathematical results back into clear, actionable business recommendations.

3. Dataset Tracks for Inspiration
The following tracks utilize standard, public datasets. They are not rigid, automated grading tests. Instead, use them as realistic inspiration to build, test, and demonstrate your agent's capability to read raw files and formulate its own optimization boundaries.

Track A: Corporate Fleet Procurement (Capital Allocation & Constraints)
The Dataset: A catalog of vehicle specifications, engine power, and fuel efficiency metrics.
Data Source (Direct CSV): https://raw.githubusercontent.com/mwaskom/seaborn-data/master/mpg.csv
The Business Scenario: A corporate logistics manager needs to purchase a new fleet of vehicles from a manufacturer's catalog. They want maximum cumulative engine power for operational capacity, but must adhere to strict corporate sustainability targets and a fixed capital budget.
The Agent's Task: The agent must load the CSV, interpret rows as available models, and map numeric columns (like horsepower, weight, and mpg) into a linear programming model to find the optimal vehicle mix without exceeding budget or emission thresholds.
Track B: Luxury Vault Stocking (Asset Portfolio Optimization)
The Dataset: A market inventory registry tracking product physical dimensions, cut quality, and wholesale market costs.
Data Source (Direct CSV): https://github.com/mwaskom/seaborn-data/blob/master/diamonds.csv
The Business Scenario: A high-end jeweler in Zurich needs to deploy a fixed line of credit to stock their retail vault. They need a diversified asset portfolio that maximizes total product mass (carats) or projected margin, while satisfying physical display case limits and strictly bounded risk categories.
The Agent's Task: The agent must sample the data dynamically, treat the price column as the cost variable, and translate qualitative text columns (like cut or clarity) into structural boundary constraints (e.g., "No single cut grade can exceed 30% of the total inventory").
Track C: Urban Dispatch Assignment (Logistics Scheduling)
The Dataset: An operational log of urban transit trips tracking distances, passenger capacities, and financial yields.
Data Source (Direct CSV): https://raw.githubusercontent.com/mwaskom/seaborn-data/master/taxis.csv
The Business Scenario: A ride-hailing or delivery platform has a localized pool of active drivers during a busy shift and a long queue of customer requests. They need to assign tasks to drivers to maximize platform revenue.
The Agent's Task: The agent must ingest an operational time slice from the data, treat rows as pending tasks, and formulate a clean routing/assignment matrix ensuring no driver receives overlapping schedules and passenger counts never exceed vehicle limits.
4. Evaluation & Presentation Criteria
Your project will be evaluated based on your final presentation, demonstrating how effectively your agent handles the messy reality of the translation loop:

Translation Fidelity: How successfully does the agent parse ambiguous, natural-language business rules and map them to correct mathematical equations?
Data Ingestion Autonomy: Can the agent inspect a raw .csv file, identify headers, handle data types, and isolate the required parameters dynamically?
Resilience & Self-Correction: If the compiled solver code crashes or returns an INFEASIBLE status, does the agent have a feedback loop to read the error, adjust its bounds, and try a secondary approach?
Business Explanation: How effectively does the agent translate raw solver arrays or binary maps back into clean, intuitive markdown summaries tailored for an executive?
