use std::path::{Path, PathBuf};

/// Resolve relative to the executable, never the Explorer/current working directory.
pub fn project_python(executable: &Path) -> Option<PathBuf> {
    executable
        .parent()?
        .ancestors()
        .take(4)
        .find_map(|directory| {
            let candidate = directory.join(".venv/Scripts/python.exe");
            candidate.is_file().then_some(candidate)
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_venv_for_dist_and_cargo_release() {
        let root = std::env::temp_dir().join(format!("tts-paths-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join(".venv/Scripts")).unwrap();
        let python = root.join(".venv/Scripts/python.exe");
        std::fs::write(&python, []).unwrap();
        assert_eq!(
            project_python(&root.join("dist/tts-host.exe")),
            Some(python.clone())
        );
        assert_eq!(
            project_python(&root.join("host/target/release/tts-host.exe")),
            Some(python)
        );
        // Remove only the unique test fixture just created under temp_dir.
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn absent_venv_is_not_invented() {
        let root = std::env::temp_dir().join(format!("tts-missing-{}", uuid::Uuid::new_v4()));
        assert!(project_python(&root.join("dist/tts-host.exe")).is_none());
    }
}
