#[derive(Default)]
pub struct KillSwitch {
    active: bool,
}

impl KillSwitch {
    pub fn new() -> Self {
        Self { active: false }
    }

    pub fn is_active(&self) -> bool {
        self.active
    }

    pub fn set(&mut self, active: bool) {
        self.active = active;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_inactive() {
        let ks = KillSwitch::new();
        assert!(!ks.is_active());
    }

    #[test]
    fn activate() {
        let mut ks = KillSwitch::new();
        ks.set(true);
        assert!(ks.is_active());
    }

    #[test]
    fn deactivate() {
        let mut ks = KillSwitch::new();
        ks.set(true);
        ks.set(false);
        assert!(!ks.is_active());
    }
}
