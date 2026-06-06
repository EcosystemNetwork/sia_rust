//! Persist an agent's submission into the standard `runs/` layout and score it
//! with the benchmark's own `evaluate.py`.
//!
//! By reusing the run-directory shape (`gen_<n>/submission.json`,
//! `agent_execution/`, `results.json`, `telemetry.json`) the Superradiant results show
//! up in the existing SIA Studio dashboard with no extra wiring: each agent gets
//! a run named `superradiant__<battle>__<agent>` whose generations are the benchmarks.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use serde_json::Value;

use crate::layout::names;
use crate::superradiant::benchmarks;
use crate::superradiant::state::AssignmentOutcome;

/// The python interpreter used to run `evaluate.py` (`$SUPERRADIANT_PYTHON` else `python3`).
fn eval_python() -> String {
    std::env::var("SUPERRADIANT_PYTHON").unwrap_or_else(|_| "python3".to_string())
}

/// Seconds before an evaluation subprocess is killed (`$SUPERRADIANT_EVAL_TIMEOUT`, default 600).
fn eval_timeout_secs() -> u64 {
    std::env::var("SUPERRADIANT_EVAL_TIMEOUT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(600)
}

fn sanitize(s: &str) -> String {
    let mut out: String = s
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    if out.is_empty() {
        out.push_str("agent");
    }
    out
}

/// Pick the next `gen_<n>` directory in a run dir.
fn next_gen_index(run_dir: &Path) -> i64 {
    let mut max = 0_i64;
    if let Ok(entries) = std::fs::read_dir(run_dir) {
        for e in entries.flatten() {
            if let Some(rest) = e.file_name().to_string_lossy().strip_prefix("gen_") {
                if let Ok(n) = rest.parse::<i64>() {
                    max = max.max(n);
                }
            }
        }
    }
    max + 1
}

/// Persist `submission` (+ optional trajectory & telemetry) for one assignment
/// and score it. Returns the scored outcome. Blocking — run on a blocking thread.
pub fn persist_and_score(
    runs_root: &Path,
    battle_id: &str,
    agent_name: &str,
    benchmark_id: &str,
    submission: &Value,
    agent_execution: Option<&Value>,
    telemetry: Option<&Value>,
) -> AssignmentOutcome {
    let task_dir = match benchmarks::task_dir_for(benchmark_id) {
        Some(d) => d,
        None => {
            return AssignmentOutcome {
                accuracy_percent: None,
                run_dir: None,
                error: Some(format!("unknown benchmark: {benchmark_id}")),
            }
        }
    };

    let run_name = format!(
        "superradiant__{}__{}",
        sanitize(battle_id),
        sanitize(agent_name)
    );
    let run_dir = runs_root.join(&run_name);
    if let Err(e) = std::fs::create_dir_all(&run_dir) {
        return outcome_err(format!("could not create run dir: {e}"));
    }
    let gen_n = next_gen_index(&run_dir);
    let gen_dir = run_dir.join(format!("gen_{gen_n}"));
    if let Err(e) = std::fs::create_dir_all(&gen_dir) {
        return outcome_err(format!("could not create gen dir: {e}"));
    }

    // Write the submission where evaluate.py will find it (submission*.json).
    let submission_path = gen_dir.join("submission.json");
    if let Err(e) = write_json(&submission_path, submission) {
        return outcome_err(format!("could not write submission: {e}"));
    }

    // A small marker so the dashboard's generation list is legible.
    let _ = std::fs::write(
        gen_dir.join(names::IMPROVEMENT_MD),
        format!("# Superradiant: {benchmark_id}\n\nAgent: {agent_name}\nBattle: {battle_id}\n"),
    );

    // Optional trajectory for the dashboard's trajectory viewer.
    if let Some(exec) = agent_execution {
        write_agent_execution(&gen_dir, exec);
    }
    if let Some(tel) = telemetry {
        let _ = write_json(&gen_dir.join("telemetry.json"), tel);
    }

    // Score with the benchmark's evaluate.py.
    let gen_dir_str = gen_dir.to_string_lossy().into_owned();
    let outcome = score(&gen_dir_str, &task_dir);
    AssignmentOutcome {
        run_dir: Some(run_dir.to_string_lossy().into_owned()),
        ..outcome
    }
}

fn score(gen_dir: &str, task_dir: &str) -> AssignmentOutcome {
    let script = match crate::layout::find_evaluate_script(task_dir) {
        Some(s) => s,
        None => return outcome_err("evaluate.py not found for benchmark".into()),
    };
    let cmd = vec![
        eval_python(),
        script,
        "--gen-dir".to_string(),
        gen_dir.to_string(),
    ];
    let log_path = format!("{gen_dir}/{}", names::EVAL_LOG);

    match run_with_timeout(&cmd, eval_timeout_secs()) {
        EvalRun::TimedOut => outcome_err(format!(
            "evaluation timed out after {}s",
            eval_timeout_secs()
        )),
        EvalRun::SpawnError(e) => outcome_err(format!("could not run evaluate.py: {e}")),
        EvalRun::Completed {
            code,
            stdout,
            stderr,
        } => {
            let _ = std::fs::write(&log_path, format!("{stdout}{stderr}"));
            if code != 0 {
                return outcome_err(format!("evaluate.py exited with code {code}"));
            }
            let results_path = format!("{gen_dir}/{}", names::RESULTS_JSON);
            match read_accuracy(&results_path) {
                Some(pct) => AssignmentOutcome {
                    accuracy_percent: Some(pct),
                    run_dir: None,
                    error: None,
                },
                None => outcome_err("results.json missing or had no accuracy".into()),
            }
        }
    }
}

/// Read accuracy from a results.json, preferring `accuracy_percent` then
/// `accuracy` (0..1, scaled to a percent).
fn read_accuracy(path: &str) -> Option<f64> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    if let Some(p) = v.get("accuracy_percent").and_then(|x| x.as_f64()) {
        return Some(p);
    }
    v.get("accuracy")
        .and_then(|x| x.as_f64())
        .map(|a| a * 100.0)
}

fn write_agent_execution(gen_dir: &Path, exec: &Value) {
    let dir = gen_dir.join(names::AGENT_EXECUTION_DIR);
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    // Accept either an array of per-question execution objects or a single object.
    let items: Vec<Value> = match exec {
        Value::Array(a) => a.clone(),
        other => vec![other.clone()],
    };
    for (i, item) in items.iter().enumerate() {
        let qid = item
            .get("question_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(i as i64);
        let _ = write_json(
            &dir.join(format!("{}{qid}.json", names::EXECUTION_GLOB_PREFIX)),
            item,
        );
    }
}

fn write_json(path: &Path, v: &Value) -> std::io::Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(v).unwrap_or_default())
}

fn outcome_err(msg: String) -> AssignmentOutcome {
    AssignmentOutcome {
        accuracy_percent: None,
        run_dir: None,
        error: Some(msg),
    }
}

enum EvalRun {
    Completed {
        code: i32,
        stdout: String,
        stderr: String,
    },
    TimedOut,
    SpawnError(String),
}

/// Run a command with a wall-clock timeout (kills the child on expiry).
fn run_with_timeout(cmd: &[String], timeout_secs: u64) -> EvalRun {
    let mut command = Command::new(&cmd[0]);
    command
        .args(&cmd[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => return EvalRun::SpawnError(e.to_string()),
    };

    // Drain pipes on a thread so a chatty child can't deadlock on a full buffer.
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let (tx, rx) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        use std::io::Read;
        let mut so = String::new();
        let mut se = String::new();
        if let Some(mut s) = stdout {
            let _ = s.read_to_string(&mut so);
        }
        if let Some(mut s) = stderr {
            let _ = s.read_to_string(&mut se);
        }
        (so, se)
    });

    let waiter = std::thread::spawn(move || {
        let status = child.wait();
        let _ = tx.send(status.map(|s| s.code().unwrap_or(-1)));
        child
    });

    match rx.recv_timeout(Duration::from_secs(timeout_secs)) {
        Ok(Ok(code)) => {
            let (so, se) = reader.join().unwrap_or_default();
            let _ = waiter.join();
            EvalRun::Completed {
                code,
                stdout: so,
                stderr: se,
            }
        }
        Ok(Err(e)) => EvalRun::SpawnError(e.to_string()),
        Err(_) => {
            // Timed out: reclaim the child handle and kill it.
            if let Ok(mut child) = waiter.join() {
                let _ = child.kill();
                let _ = child.wait();
            }
            let _ = reader.join();
            EvalRun::TimedOut
        }
    }
}

/// Convenience used by callers that already hold the runs root as a `&str`.
pub fn runs_root_path(runs_root: &str) -> PathBuf {
    PathBuf::from(runs_root)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_benchmark_errors_cleanly() {
        let tmp = std::env::temp_dir();
        let out = persist_and_score(
            &tmp,
            "b1",
            "agent",
            "nope-xyz",
            &serde_json::json!({}),
            None,
            None,
        );
        assert!(out.error.is_some());
        assert!(out.accuracy_percent.is_none());
    }

    #[test]
    fn accuracy_parsed_from_percent_or_fraction() {
        let dir = std::env::temp_dir().join("superradiant_acc_test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("results.json");
        std::fs::write(&p, r#"{"accuracy_percent": 42.5}"#).unwrap();
        assert_eq!(read_accuracy(p.to_str().unwrap()), Some(42.5));
        std::fs::write(&p, r#"{"accuracy": 0.25}"#).unwrap();
        assert_eq!(read_accuracy(p.to_str().unwrap()), Some(25.0));
    }
}
