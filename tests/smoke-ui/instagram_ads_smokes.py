"""Focused guide and exact-request rendering; all backend data is mocked."""
import json

from playwright.sync_api import expect


def run(page, url, log_in, open_home_integration):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    log_in(page, url)
    open_home_integration(page, 'tool:instagram_ads')
    guide = page.locator("[data-guide-section='tool:instagram_ads']")
    expect(page.locator("#integration-detail-logo [data-integration-logo='tool:instagram_ads']")).to_have_attribute('data-logo-source', 'brand')
    expect(guide).to_contain_text('separate from the working organic Instagram connection')
    expect(guide).to_contain_text('Creator identities can use the other three outcomes')
    expect(guide).to_contain_text('activates parent last')
    expect(guide).to_contain_text('later release delivery within the approved flight')
    expect(guide).to_contain_text('Meta ad spend is separate')
    expect(guide).to_contain_text('Meta Ads Manager > Advertising settings')
    expect(guide).to_contain_text('fresh geography/optional interests')
    expect(guide).not_to_contain_text('list_audiences')
    expect(guide).not_to_contain_text('audience_ids')
    expect(guide.locator('.guide-capability')).to_have_count(11)
    expect(guide).to_contain_text('Missing fields alone do not prove missing Meta permissions')
    diagnostic = guide.locator('.guide-capability').filter(has=page.locator('h4 code', has_text='diagnose_account'))
    diagnostic.locator('.guide-action-contract > summary').click()
    expect(diagnostic.locator('.guide-action-contract > summary')).to_contain_text('4 inputs')
    expect(diagnostic).to_contain_text('runs directly')
    for field in ('pages_after', 'instagram_after', 'user_tasks_state', 'launch_task_check_passes', 'instagram_accounts', 'error_code'):
        expect(diagnostic).to_contain_text(field)
    expect(page.locator('#tool-config-instagram_ads-INSTAGRAM_ADS_APP_SECRET')).to_have_attribute('type', 'password')
    capability = guide.locator('.guide-capability').filter(has=page.locator('h4 code', has_text='launch_campaign'))
    capability.locator('.guide-action-contract > summary').click()
    expect(capability.locator('.guide-action-contract > summary')).to_contain_text('12 inputs')
    expect(capability).to_contain_text('lifetime_budget')
    expect(capability).to_contain_text('special_ad_category')
    expect(capability).to_contain_text('PROFILE_VISITS')
    expect(capability).to_contain_text('Fresh ad-set targeting, without saved custom/lookalike audience objects')
    input_parameters = capability.locator('.guide-action-contract-body section').first
    expect(input_parameters).not_to_contain_text('dsa_beneficiary')
    expect(input_parameters).not_to_contain_text('dsa_payor')
    caption = '<script>window.unapprovedContent = true</script> Original caption & tags'
    payload = {'tool_id': 'instagram_ads', 'proposal': {
        'input': {'account_id': '100', 'page_id': '200', 'instagram_user_id': '300', 'media_id': '400',
                  'objective': 'WEBSITE_CLICKS', 'lifetime_budget': '2500', 'special_ad_category': 'NONE',
                  'start_time': '2099-01-01T00:00:00Z', 'end_time': '2099-01-03T00:00:00Z'},
        'account': {'currency': 'USD', 'currency_offset': 100},
        'source_reel': {'caption': caption, 'media_url': 'https://cdninstagram.com/exact.mp4?signature=approved',
                        'permalink': 'https://www.instagram.com/reel/owned/'},
        'dsa': {'dsa_beneficiary': 'Exact public beneficiary', 'dsa_payor': 'Exact public payer'},
        'audience': {'targeting': {'geo_locations': {'countries': ['DE']}, 'publisher_platforms': ['instagram'],
                                 'instagram_positions': ['reels'], 'targeting_automation': {'advantage_audience': 1}}},
        'call_to_action': {'type': 'LEARN_MORE', 'value': {'link': 'https://example.com/exact?campaign=reel'}},
    }}
    row = {'id': 'meta-review', 'kind': 'tool', 'source': 'Instagram Ads', 'tool_id': 'instagram_ads',
           'action_id': 'launch_campaign', 'status': 'pending', 'summary': 'New Reel campaign, USD 25.00 total, parent activates last.',
           'created_at': 1788960000, 'updated_at': 1788960000}
    page.route('**/v1/approvals?*', lambda route: route.fulfill(json={
        'items': [row], 'page': 1, 'pages': 1, 'page_size': 25, 'total': 1,
        'pending_count': 1, 'history_count': 0,
    }))
    page.route('**/v1/tools/instagram_ads/approvals/meta-review', lambda route: route.fulfill(json={'approval': {'payload': payload}}))
    if page.locator('#mobile-nav-toggle').is_visible():
        page.locator('#mobile-nav-toggle').click()
    page.get_by_role('button', name='Approvals', exact=False).click()
    card = page.locator("[data-approval-key='tool:meta-review']")
    card.get_by_text('View exact request', exact=True).click()
    pre = card.locator('.approval-payload')
    expect(pre).to_contain_text(caption)
    expect(pre).to_contain_text('Exact public beneficiary')
    expect(pre).to_contain_text('Exact public payer')
    assert json.loads(pre.inner_text()) == payload
    assert page.evaluate('window.unapprovedContent') is None
    assert not errors, errors
    print('Instagram Ads guide and exact caption/source/CTA/targeting/budget/flight review render safely', flush=True)
