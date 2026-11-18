import time

from test_ops_supervisor import env, _supervisor


class Child:
    pid=123
    terminated=False
    def poll(self):
        return 0 if self.terminated else None
    def terminate(self):
        self.terminated=True
    def send_signal(self,signal):
        self.terminated=True
    def wait(self,timeout=None):
        return 0


def test_supervisor_renews_channel_lease_until_shutdown(env,monkeypatch):
    child=Child()
    supervisor=_supervisor(env,spawner=lambda *args:child,drain=lambda *args,**kwargs:{})
    supervisor.start(preflight_report={'ok':True})
    supervisor.guard.lease_seconds=0.12
    calls=[]
    monkeypatch.setattr(supervisor.guard,'renew',lambda:calls.append(time.monotonic()) or True)
    end=time.monotonic()+0.18
    result=supervisor.supervise(poll_interval=0.01,should_continue=lambda:time.monotonic()<end)
    assert result['ok'] and len(calls)>=2
    assert child.terminated and not supervisor.guard.holder


def test_lost_channel_lease_stops_the_actual_child(env,monkeypatch):
    child=Child()
    supervisor=_supervisor(env,spawner=lambda *args:child,drain=lambda *args,**kwargs:{})
    supervisor.start(preflight_report={'ok':True})
    supervisor.guard.lease_seconds=0.12
    monkeypatch.setattr(supervisor.guard,'renew',lambda:False)
    result=supervisor.supervise(poll_interval=0.01)
    assert not result['ok'] and result['reason_code']=='single_active_lost'
    assert child.terminated and not supervisor.guard.holder
