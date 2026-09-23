# Phase 1: Grid and Network Attack Dataset Generation

## 1. Goal of Phase 1

The goal of Phase 1 is to build a system that can take different electrical feeder models, run normal grid activity using realistic load and solar data, simulate cyber attacks, and produce a labelled attack dataset.

The important design goal is:

> The attack system should not be built for only one feeder.

A new feeder model should be usable by adding a small adapter that converts it into the common format expected by the system.

In simple terms:

```text
Feeder Model + Ausgrid Data
            |
            v
     Common Grid Model
            |
            v
      Normal Simulation
            |
            v
       Attack Engine
            |
            v
    Attacked Simulation
            |
            v
   Cyber + Grid Dataset
```

---

## 2. What We Already Have

### Ausgrid data

The Ausgrid data gives us real electricity usage and solar-generation patterns over time.

It tells us things such as:

- how much electricity is being used
- how much solar power is being produced
- how these values change during the day
- how they change across different days

We use this data to make the simulated grid behave more realistically.

### Feeder models

We have multiple feeder models, such as IEEE feeder models.

A feeder model describes the structure of a distribution network:

- buses/nodes
- lines
- transformers
- loads
- solar/generators
- switches
- voltage regulators
- capacitors
- other devices

The important point is that the project should not depend on one specific feeder.

---

# 3. Main Architecture

```text
                     INPUT DATA
                         |
          +--------------+--------------+
          |                             |
          v                             v
   Ausgrid Data                   Feeder Model
   Load / Solar                   Any Supported Feeder
          |                             |
          v                             v
   Profile Engine                Feeder Adapter
          |                             |
          +-------------+---------------+
                        |
                        v
               Common Grid Model
                        |
                        v
                Normal Simulation
                        |
                        v
                Normal Grid State
                        |
                        v
                Attack Engine
                        |
              +---------+---------+
              |                   |
              v                   v
        Cyber Attack         MITRE Mapping
              |                   |
              +---------+---------+
                        |
                        v
                Attack Simulation
                        |
              +---------+---------+
              |                   |
              v                   v
        Network Data          Grid Data
              |                   |
              +---------+---------+
                        |
                        v
                 Ground Truth
                        |
                        v
                 Attack Dataset
```

---

# 4. Step 1: Read the Feeder

The system first receives a feeder model.

For example:

```text
IEEE-37
```

or:

```text
IEEE-123
```

or another supported feeder.

The system reads the feeder and converts it into a common format.

Instead of writing an attack specifically for:

```text
IEEE123_Regulator_4
```

the system works with:

```text
Voltage Regulator
```

For example:

```text
Find a voltage regulator
        |
        v
Select one available regulator
        |
        v
Change its tap position
```

This means the same attack logic can work on different feeder models.

### Feeder Adapter

Different feeder models may store information differently.

Therefore, each feeder format gets a small adapter:

```text
IEEE-37 --------IEEE-123 --------> Feeder Adapter ---> Common Grid Model
OpenDSS --------/
Other feeder ---/
```

The rest of the project does not need to know where the feeder came from.

---

# 5. Step 2: Add Realistic Load and Solar Behavior

The Ausgrid data is used to create profiles.

For example:

```text
Time       Load       Solar
08:00      1.2 kW     0.3 kW
09:00      1.5 kW     0.7 kW
10:00      1.8 kW     1.2 kW
11:00      2.0 kW     1.8 kW
```

These profiles are assigned to suitable loads or generation points in the feeder.

The mapping should be configurable.

We should not claim that an Ausgrid household is the real-world equivalent of a particular IEEE bus.

Instead, we create different assignment strategies, for example:

```text
Ausgrid profiles
       |
       +-- Random assignment
       |
       +-- Clustered assignment
       |
       +-- Rule-based assignment
       |
       v
    Feeder loads
```

This allows us to create many realistic operating conditions.

---

# 6. Step 3: Generate Normal Grid Data

Before generating attacks, the system must generate normal data.

This gives us a baseline.

For every simulation step we record values such as:

```text
timestamp
node
voltage
current
active power
reactive power
load
solar generation
switch state
regulator state
transformer loading
```

Example:

```text
14:00
Node 12
Voltage = 0.99 pu
Load = 1.4 MW
Solar = 0.8 MW
Switch = Closed
```

This is labelled:

```text
NORMAL
```

We need normal data because later we need to know what changed when an attack occurred.

---

# 7. Step 4: Create the Network Layer

The project should also simulate the communication side of the system.

We do not need to build a complete real SCADA environment for Phase 1.

We can start with simple messages between devices.

For example:

```text
Controller
     |
     | SET_TAP = 3
     v
Regulator
```

or:

```text
RTU
 |
 | voltage = 0.99
 v
Controller
```

The network layer records things such as:

```text
timestamp
source
destination
message type
command
value
connection state
```

This gives us network information alongside the grid information.

---

# 8. Step 5: Attack Engine

The attack engine is responsible for creating attacks.

The important design rule is:

> An attack should target a type of asset or action, not a hard-coded feeder component.

Bad design:

```text
Attack IEEE123_Regulator_4
```

Better design:

```text
Find voltage regulator
        |
        v
Select target
        |
        v
Change tap position
```

The same idea can be used for other devices.

Examples:

```text
Find switch
    -> change switch state

Find regulator
    -> change tap position

Find measurement
    -> change reported value

Find communication path
    -> block communication

Find controllable parameter
    -> modify parameter
```

This makes the attack engine reusable.

---

# 9. MITRE ATT&CK Mapping

Each simulated attack is connected to a MITRE ATT&CK for ICS technique.

The MITRE label describes what kind of attacker action we are simulating.

For example:

```text
Action
Change a device parameter
        |
        v
MITRE technique
Modify Parameter
```

Another example:

```text
Action
Send an unauthorized control command
        |
        v
MITRE technique
Unauthorized Command Message
```

MITRE is therefore used as the common language for describing the attacks.

---

# 10. Attack Behavior

The system should not assume that every attacker follows exactly the same path.

Some scenarios may start with reconnaissance:

```text
Access
  |
  v
Recon
  |
  v
Find target
  |
  v
Attack
```

But another scenario may be:

```text
Access
  |
  v
Immediately attack available target
```

Another could be:

```text
Access
  |
  v
Recon
  |
  v
Attack
  |
  v
Observe result
  |
  v
Second attack
```

Therefore, attack behavior should be configurable.

---

# 11. Phase 1 Attack Scenarios

We should start with a small number of scenarios instead of trying to simulate every possible attack.

## Scenario 1: Normal Operation

No attack.

```text
Load/Solar Data
      |
      v
Feeder
      |
      v
Normal Simulation
      |
      v
Normal Dataset
```

Purpose:

Create the baseline.

---

## Scenario 2: Reconnaissance

The attacker tries to learn what devices or points are available.

Example:

```text
Attacker
   |
   v
Discover devices
   |
   v
Find regulator
Find switch
Find measurements
```

The grid may not change at all.

The dataset records the network activity.

Purpose:

Create examples where malicious activity exists but there may be little or no physical effect.

---

## Scenario 3: Unauthorized Command

The attacker sends a command that should not be sent.

Example:

```text
Attacker
    |
    | change switch state
    v
Switch
    |
    v
Grid changes
```

The dataset contains both:

```text
Network event
+
Grid response
```

---

## Scenario 4: Modify a Device Parameter

Example:

```text
Find voltage regulator
        |
        v
Change tap position
        |
        v
Run grid simulation
        |
        v
Voltage changes
```

The exact regulator depends on the feeder.

The attack code does not contain a hard-coded IEEE-37 or IEEE-123 device ID.

---

## Scenario 5: False Measurement

The attacker changes what the controller sees.

Example:

```text
Actual voltage
     |
     v
0.94 pu

Attacker changes reported value
     |
     v
0.99 pu
```

The physical grid may be unhealthy while the reported measurement looks normal.

This gives us an important attack dataset because the network data and physical data can tell different stories.

---

## Scenario 6: Communication Disruption

The attacker prevents or delays messages.

Example:

```text
Controller
    |
    X
    |
   RTU
```

The grid continues operating, but the controller cannot communicate properly with the device.

The dataset records:

```text
connection failure
message loss
command failure
device state
grid state
```

---

## Scenario 7: Multi-Step Attack

Several actions are combined.

For example:

```text
Access
  |
  v
Recon
  |
  v
Find regulator
  |
  v
Modify parameter
  |
  v
Observe result
  |
  v
Modify again
```

This allows us to create attack timelines rather than isolated malicious rows.

---

# 12. How One Attack Becomes a Dataset

Suppose we simulate:

```text
Modify regulator tap
```

The system first runs normally:

```text
14:00  Voltage = 0.99
14:01  Voltage = 0.99
14:02  Voltage = 0.98
```

At 14:03 the attack begins:

```text
14:03  Attacker changes tap position
```

The system runs the feeder again with the changed parameter.

Maybe the resulting state becomes:

```text
14:03  Voltage = 1.01
14:04  Voltage = 1.03
14:05  Voltage = 1.04
```

The dataset records the entire sequence.

Conceptually:

```text
NORMAL
  |
  | 14:03
  v
ATTACK START
  |
  v
DEVICE CHANGE
  |
  v
GRID RESPONSE
  |
  v
ATTACK END
  |
  v
RECOVERY
```

This is much more useful than simply changing a random value in a CSV.

---

# 13. The Dataset Will Have Two Main Sides

## Network side

Records what happened in communication.

```text
timestamp
source
destination
message
command
connection
attack event
MITRE technique
```

## Grid side

Records what happened in the feeder.

```text
timestamp
node
voltage
current
power
load
solar
switch state
regulator state
transformer loading
```

They share the same timestamp and scenario ID.

Therefore we can connect:

```text
Network Event
      |
      v
Attack Action
      |
      v
Grid Change
```

---

# 14. Ground Truth

Every generated scenario needs a clear answer to:

> Was there an attack, what attack was it, where did it happen, and when did it happen?

For example:

```text
scenario_id: SCN_0012

feeder: IEEE37

attack: Modify Parameter

target: Voltage Regulator

start: 14:03

end: 14:07

MITRE: T0836

is_attack: true
```

We also record the physical effect:

```text
voltage_before = 0.99
voltage_after  = 1.04
```

This is called ground truth.

It tells us what actually happened in the simulation.

---

# 15. Generate Many Variations

We should not generate one copy of each attack.

The same attack should be tested under different conditions.

For example:

```text
                    Modify Parameter
                           |
          +----------------+----------------+
          |                |                |
       Low change       Medium change    High change
          |                |                |
       Feeder A         Feeder A         Feeder A
       Feeder B         Feeder B         Feeder B
       Feeder C         Feeder C         Feeder C
```

We can also vary:

- time of day
- load level
- solar generation
- target device
- attack duration
- attack intensity
- feeder model
- starting grid condition

This creates a much larger and more useful dataset.

---

# 16. Why Multiple Feeder Models Matter

Suppose we generate attacks only on IEEE-37.

A detection system might learn:

```text
IEEE-37 + Attack
```

instead of learning:

```text
Attack behavior
```

With several feeder models we can test whether the generated attack patterns remain useful across different network structures.

For example:

```text
Training:
IEEE-37 + IEEE-69

Testing:
IEEE-123
```

This becomes a way to test whether the later detection system works on a feeder it has not seen before.

---

# 17. Phase 1 Final Pipeline

The complete Phase 1 process is:

```text
1. Load Ausgrid data
        |
        v
2. Load feeder model
        |
        v
3. Convert feeder into common format
        |
        v
4. Assign load/solar profiles
        |
        v
5. Run normal grid simulation
        |
        v
6. Generate normal network activity
        |
        v
7. Select attack scenario
        |
        v
8. Select MITRE technique
        |
        v
9. Select target dynamically
        |
        v
10. Perform attack
        |
        v
11. Run grid/network simulation
        |
        v
12. Record network changes
        |
        v
13. Record grid changes
        |
        v
14. Add attack labels
        |
        v
15. Validate the scenario
        |
        v
16. Export attack dataset
```

---

# 18. Phase 1 Deliverables

At the end of Phase 1, we should have:

### 1. Feeder adapter system

Able to accept multiple feeder models.

### 2. Common feeder representation

A standard way to represent:

```text
nodes
lines
loads
generators
switches
regulators
transformers
```

### 3. Ausgrid profile engine

Converts the real data into usable load and solar profiles.

### 4. Normal scenario generator

Produces realistic normal operating data.

### 5. Network simulator

Produces basic communication and command data.

### 6. MITRE attack engine

Generates selected ICS attack scenarios.

### 7. Scenario generator

Creates many variations of each attack.

### 8. Ground-truth generator

Labels exactly when, where, and how an attack occurred.

### 9. Final datasets

Something like:

```text
datasets/
│
├── normal/
│   ├── feeder_37/
│   ├── feeder_69/
│   └── feeder_123/
│
├── attacks/
│   ├── reconnaissance/
│   ├── unauthorized_command/
│   ├── parameter_modification/
│   ├── false_measurement/
│   ├── communication_disruption/
│   └── multi_stage/
│
└── metadata/
    ├── scenarios.csv
    ├── mitre_mapping.csv
    └── ground_truth.csv
```

---

# 19. The Simple Idea Behind the Whole Project

The entire Phase 1 can be remembered as five steps:

```text
FEEDER
   ↓
Make it understandable to the system

PROFILE
   ↓
Give it realistic load and solar behavior

NORMAL
   ↓
Record what normal operation looks like

ATTACK
   ↓
Change something using a defined MITRE scenario

RECORD
   ↓
Save what happened on the network AND grid
```

The final objective is therefore not simply:

> "Generate fake attack data."

It is:

> **Take different feeder models, give them realistic operating conditions, simulate controlled attacks, observe what changes in both the network and grid, and save those events with accurate MITRE and attack labels.**

That gives Phase 1 a clean foundation for the later detection, MITRE coverage, and mitigation phases.
