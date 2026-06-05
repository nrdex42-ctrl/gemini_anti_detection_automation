from fb_automation.stochastic_timer import AdvancedStochasticTimer, TimingProfile


def test_advanced_stochastic_timer_compat_surface():
    timer = AdvancedStochasticTimer(seed=123)

    assert hasattr(TimingProfile, 'CAUTIOUS')
    assert hasattr(TimingProfile, 'BALANCED')

    think_delay = timer.think_time(100, 500)
    short_typing = timer.type_text_time('hello', profile=TimingProfile.CAUTIOUS)
    long_typing = timer.type_text_time('hello world this is longer', profile=TimingProfile.CAUTIOUS)

    assert 0.1 <= think_delay <= 0.5
    assert short_typing > 0
    assert long_typing > short_typing
