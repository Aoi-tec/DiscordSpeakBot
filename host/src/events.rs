use std::sync::{mpsc, Arc};

/// Wake the native event loop after enqueueing, including while the window is hidden.
pub struct WakeSender<T> {
    sender: mpsc::Sender<T>,
    wake: Arc<dyn Fn() + Send + Sync>,
}

impl<T> WakeSender<T> {
    pub fn new(sender: mpsc::Sender<T>, wake: impl Fn() + Send + Sync + 'static) -> Self {
        Self {
            sender,
            wake: Arc::new(wake),
        }
    }

    pub fn send(&self, event: T) -> Result<(), mpsc::SendError<T>> {
        self.sender.send(event)?;
        (self.wake)();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn queued_event_wakes_idle_ui() {
        let (tx, rx) = mpsc::channel();
        let calls = Arc::new(AtomicUsize::new(0));
        let counted = calls.clone();
        let sender = WakeSender::new(tx, move || {
            counted.fetch_add(1, Ordering::SeqCst);
        });
        sender.send("exit").unwrap();
        assert_eq!(rx.try_recv().unwrap(), "exit");
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        sender.send("stopped").unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }
}
