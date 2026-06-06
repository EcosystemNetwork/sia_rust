//! Benchmark discovery for the Superradiant.
//!
//! A "benchmark" is just a SIA task directory (the same ones `sia run --task`
//! uses): a folder containing `data/public/task.md` and `data/public/evaluate.py`.
//! Agents fetch the public spec + data files, run the task, and post a
//! submission that the task's own `evaluate.py` scores.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::layout::names;

/// A selectable benchmark backed by an on-disk task directory.
#[derive(Debug, Clone, Serialize)]
pub struct BenchmarkRef {
    pub id: String,
    pub title: String,
    /// Absolute or repo-relative task directory.
    pub task_dir: String,
    pub has_evaluator: bool,
}

/// Root directory holding tasks: `$SIA_TASKS_DIR` else `./sia/tasks`.
fn tasks_root() -> PathBuf {
    match std::env::var("SIA_TASKS_DIR") {
        Ok(v) => PathBuf::from(v),
        Err(_) => PathBuf::from("sia/tasks"),
    }
}

fn is_task_dir(dir: &Path) -> bool {
    dir.join(names::TASK_MD).is_file()
}

/// Discover every task directory under the tasks root that looks like a
/// benchmark (has a public `task.md`).
pub fn discover_benchmarks() -> Vec<BenchmarkRef> {
    let root = tasks_root();
    let mut out: Vec<BenchmarkRef> = Vec::new();
    let Ok(entries) = std::fs::read_dir(&root) else {
        return out;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('_') || name.starts_with('.') {
            continue;
        }
        if !is_task_dir(&path) {
            continue;
        }
        let has_evaluator = crate::layout::find_evaluate_script(&path.to_string_lossy()).is_some();
        out.push(BenchmarkRef {
            id: name.clone(),
            title: title_from_task_md(&path).unwrap_or(name),
            task_dir: path.to_string_lossy().into_owned(),
            has_evaluator,
        });
    }
    out.sort_by(|a, b| a.id.cmp(&b.id));
    out
}

/// Resolve a benchmark id to its task directory, validating it still exists.
pub fn task_dir_for(id: &str) -> Option<String> {
    if id.is_empty() || id.contains(['/', '\\', '.']) {
        return None;
    }
    let dir = tasks_root().join(id);
    if is_task_dir(&dir) {
        Some(dir.to_string_lossy().into_owned())
    } else {
        None
    }
}

/// The public `task.md` text for a benchmark.
pub fn task_md(id: &str) -> Option<String> {
    let dir = task_dir_for(id)?;
    std::fs::read_to_string(Path::new(&dir).join(names::TASK_MD)).ok()
}

/// List the public data file paths (relative to `data/public`) an agent may
/// fetch, excluding the evaluator itself.
pub fn public_files(id: &str) -> Vec<String> {
    let Some(dir) = task_dir_for(id) else {
        return Vec::new();
    };
    let public = Path::new(&dir).join(names::DATA_PUBLIC);
    let mut out = Vec::new();
    collect_files(&public, &public, &mut out);
    out.retain(|p| p != names::EVALUATE_PY);
    out.sort();
    out
}

/// Read a single public data file, guarding against path traversal.
pub fn read_public_file(id: &str, rel: &str) -> Option<Vec<u8>> {
    let dir = task_dir_for(id)?;
    // Reject traversal / absolute components.
    if rel
        .split(['/', '\\'])
        .any(|c| c == ".." || c.is_empty() || c == ".")
    {
        return None;
    }
    if Path::new(rel).is_absolute() {
        return None;
    }
    let public = Path::new(&dir).join(names::DATA_PUBLIC);
    let target = public.join(rel);
    // Never hand back the evaluator.
    if target
        .file_name()
        .map(|n| n == names::EVALUATE_PY)
        .unwrap_or(false)
    {
        return None;
    }
    // Confirm the resolved path stays inside the public dir.
    let canon_public = std::fs::canonicalize(&public).ok()?;
    let canon_target = std::fs::canonicalize(&target).ok()?;
    if !canon_target.starts_with(&canon_public) {
        return None;
    }
    std::fs::read(&canon_target).ok()
}

fn collect_files(base: &Path, dir: &Path, out: &mut Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_files(base, &path, out);
        } else if let Ok(rel) = path.strip_prefix(base) {
            out.push(rel.to_string_lossy().replace('\\', "/"));
        }
    }
}

fn title_from_task_md(task_dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(task_dir.join(names::TASK_MD)).ok()?;
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("# ") {
            return Some(rest.trim().to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn traversal_is_rejected() {
        assert!(read_public_file("gpqa", "../../../etc/passwd").is_none());
        assert!(read_public_file("gpqa", "/etc/passwd").is_none());
    }

    #[test]
    fn unknown_benchmark_has_no_dir() {
        assert!(task_dir_for("definitely-not-a-task").is_none());
        // Path-ish ids are rejected outright.
        assert!(task_dir_for("../secrets").is_none());
    }
}
