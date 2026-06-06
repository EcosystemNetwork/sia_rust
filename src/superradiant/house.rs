//! In-process "house" competitor: runs a user-supplied LLM directly against a
//! benchmark, server-side, so two models (e.g. Google's Gemini vs an
//! OpenAI-compatible custom endpoint) can be pitted head-to-head from the UI
//! without an external worker.
//!
//! Gated behind `superradiant-db` (which implies `llm`). v1 supports
//! **multiple-choice** benchmarks (e.g. `arithmetic-mc`, `gpqa`) — the shape
//! `{"id", "Question", "options": {"A".."D"}}` with a submission contract of
//! `{"model", "total_questions", "details": [{"question_id", "model_answer"}]}`.
//! Non-MC tasks return a clear error and stay external-worker-only.
//!
//! Scoring + persistence reuse [`crate::superradiant::eval::persist_and_score`]
//! verbatim, so house runs show up in SIA Studio like any other run.

use std::path::Path;

use serde_json::{json, Value};

use crate::error::SiaResult;
use crate::llm::{
    client_for_with_key, AgentClient, ApiMessage, ChatMessage, ChatRequest, ChatTransport,
    ContentBlock, MessagesRequest, MessagesTransport,
};
use crate::superradiant::benchmarks;
use crate::superradiant::credentials::ResolvedCredential;
use crate::superradiant::eval;
use crate::superradiant::state::{AssignmentOutcome, SuperradiantRunConfig};
use crate::verifier::extract_choice_letter;

/// Per-question token budget for the model's answer. Generous enough for a brief
/// rationale; the answer letter is then extracted from the full text.
const ANSWER_MAX_TOKENS: u64 = 512;

fn err(msg: String) -> AssignmentOutcome {
    AssignmentOutcome {
        accuracy_percent: None,
        run_dir: None,
        error: Some(msg),
    }
}

/// Run one (credential × benchmark) assignment in-process and score it.
///
/// Blocking (uses the synchronous `reqwest::blocking` transports) — call from a
/// blocking thread (`tokio::task::spawn_blocking`).
pub fn run_house_assignment(
    cred: &ResolvedCredential,
    runs_root: &Path,
    battle_id: &str,
    agent_name: &str,
    benchmark_id: &str,
    config: &SuperradiantRunConfig,
) -> AssignmentOutcome {
    if benchmarks::task_dir_for(benchmark_id).is_none() {
        return err(format!("unknown benchmark: {benchmark_id}"));
    }
    let questions = match load_mc_questions(benchmark_id) {
        Some(q) if !q.is_empty() => q,
        _ => {
            return err(format!(
                "benchmark '{benchmark_id}' is not a supported multiple-choice task \
                 for in-process competitors yet (use an external worker)"
            ))
        }
    };

    // Admin's suggested model overrides the credential's default when set.
    let model = if config.model_name.trim().is_empty() {
        cred.model.clone()
    } else {
        config.model_name.trim().to_string()
    };

    let client =
        match client_for_with_key(&cred.client_kind, cred.base_url.as_deref(), cred.api_key.clone())
        {
            Ok(c) => c,
            Err(e) => return err(format!("could not build LLM client: {e}")),
        };

    let system = "You are a careful exam-taker answering multiple-choice questions.";
    let mut details: Vec<Value> = Vec::new();
    let mut execs: Vec<Value> = Vec::new();
    let (mut in_tok, mut out_tok) = (0u64, 0u64);

    for (i, q) in questions.iter().enumerate() {
        let qid = q
            .get("id")
            .and_then(|x| x.as_i64())
            .unwrap_or((i + 1) as i64);
        let prompt = build_prompt(q);
        match ask(&client, &model, system, &prompt) {
            Ok(ans) => {
                in_tok += ans.input_tokens;
                out_tok += ans.output_tokens;
                let letter = extract_choice_letter(&ans.text);
                details.push(json!({ "question_id": qid, "model_answer": letter }));
                execs.push(json!({
                    "question_id": qid,
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": prompt}]},
                        {"role": "assistant", "content": [{"type": "text", "text": ans.text}]}
                    ]
                }));
            }
            Err(e) => return err(format!("LLM call failed on question {qid}: {e}")),
        }
    }

    let submission = json!({
        "model": model,
        "total_questions": details.len(),
        "details": details,
    });
    let telemetry = json!({
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
    });
    let exec = Value::Array(execs);

    eval::persist_and_score(
        runs_root,
        battle_id,
        agent_name,
        benchmark_id,
        &submission,
        Some(&exec),
        Some(&telemetry),
    )
}

/// The model's answer to one question plus token usage.
struct LlmAnswer {
    text: String,
    input_tokens: u64,
    output_tokens: u64,
}

/// Send one single-shot (no tools) completion and return the text + usage.
fn ask(client: &AgentClient, model: &str, system: &str, prompt: &str) -> SiaResult<LlmAnswer> {
    match client {
        AgentClient::Anthropic(t) => {
            let req = MessagesRequest {
                model: model.to_string(),
                max_tokens: ANSWER_MAX_TOKENS,
                messages: vec![ApiMessage::user_text(prompt)],
                tools: vec![],
                system: Some(system.to_string()),
            };
            let resp = t.create_message(&req)?;
            let text = resp
                .content
                .iter()
                .find_map(|b| match b {
                    ContentBlock::Text { text } => Some(text.clone()),
                    _ => None,
                })
                .unwrap_or_default();
            Ok(LlmAnswer {
                text,
                input_tokens: resp.usage.input_tokens,
                output_tokens: resp.usage.output_tokens,
            })
        }
        AgentClient::Chat(t) => {
            let req = ChatRequest {
                model: model.to_string(),
                messages: vec![
                    ChatMessage {
                        role: "system".to_string(),
                        content: Some(system.to_string()),
                        tool_calls: vec![],
                        tool_call_id: None,
                    },
                    ChatMessage::user(prompt),
                ],
                tools: vec![],
                tool_choice: None,
                max_tokens: Some(ANSWER_MAX_TOKENS),
            };
            let resp = t.create(&req)?;
            let text = resp
                .choices
                .first()
                .and_then(|c| c.message.content.clone())
                .unwrap_or_default();
            Ok(LlmAnswer {
                text,
                input_tokens: resp.usage.prompt_tokens,
                output_tokens: resp.usage.completion_tokens,
            })
        }
    }
}

/// True if a JSON object looks like a multiple-choice question.
fn is_mc_question(v: &Value) -> bool {
    v.get("Question").and_then(|x| x.as_str()).is_some()
        && v.get("options")
            .and_then(|o| o.as_object())
            .map(|o| o.contains_key("A") && o.contains_key("B"))
            .unwrap_or(false)
}

/// Find the benchmark's public questions file: the first public `*.json` that
/// parses as an array of MC questions (handles `questions.json`,
/// `diamond_questions.json`, etc.).
fn load_mc_questions(benchmark_id: &str) -> Option<Vec<Value>> {
    for rel in benchmarks::public_files(benchmark_id) {
        if !rel.ends_with(".json") {
            continue;
        }
        let Some(bytes) = benchmarks::read_public_file(benchmark_id, &rel) else {
            continue;
        };
        let Ok(Value::Array(arr)) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        if arr.first().map(is_mc_question).unwrap_or(false) {
            return Some(arr);
        }
    }
    None
}

/// Render a question into a strict "answer with one letter" prompt.
fn build_prompt(q: &Value) -> String {
    let question = q.get("Question").and_then(|x| x.as_str()).unwrap_or("");
    let mut s = format!("Question: {question}\n\nOptions:\n");
    if let Some(opts) = q.get("options").and_then(|o| o.as_object()) {
        for letter in ["A", "B", "C", "D"] {
            if let Some(val) = opts.get(letter).and_then(|x| x.as_str()) {
                s.push_str(&format!("{letter}. {val}\n"));
            }
        }
    }
    s.push_str("\nRespond with ONLY the single letter (A, B, C, or D) of the correct option.");
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_mc_shape() {
        let q = json!({"id": 1, "Question": "2+2?", "options": {"A":"3","B":"4","C":"5","D":"6"}});
        assert!(is_mc_question(&q));
        let not = json!({"prompt": "hi"});
        assert!(!is_mc_question(&not));
    }

    #[test]
    fn prompt_lists_all_options() {
        let q = json!({"Question": "2+2?", "options": {"A":"3","B":"4","C":"5","D":"6"}});
        let p = build_prompt(&q);
        assert!(p.contains("A. 3") && p.contains("D. 6"));
        assert!(p.contains("single letter"));
    }

    #[test]
    fn unknown_benchmark_errors() {
        let cred = ResolvedCredential {
            id: "x".into(),
            name: "x".into(),
            client_kind: "openai".into(),
            base_url: None,
            model: "m".into(),
            api_key: "k".into(),
        };
        let out = run_house_assignment(
            &cred,
            std::env::temp_dir().as_path(),
            "b1",
            "house",
            "definitely-not-a-task",
            &SuperradiantRunConfig::default(),
        );
        assert!(out.error.is_some());
    }
}
