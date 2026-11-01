-- Shopper contact and appointment details from a submissions export.
-- Expects a `submissions` view over an export written with --normalize-headers;
-- scripts/shopper_list.sh sets that up.
SELECT
    please_enter_your_na as shopper_name,
    phone_number,
    email_address,
    how_many_folks_are_y as folks,
    will_you_need_a_volu as assistance,
    -- Answers are "Yes!" or "Not right now"; anything else (including blank) is NULL.
    CASE can_we_add_you_to_ou
        WHEN 'Yes!' THEN true
        WHEN 'Not right now' THEN false
    END AS opt_in,
    copy_of_appt_time as appt_time,
    -- e.g. "10/31/2026 2:30 PM" -> 870; NULL if the time can't be parsed.
    (
        hour(try_strptime(copy_of_appt_time, '%m/%d/%Y %I:%M %p')) * 60
        + minute(try_strptime(copy_of_appt_time, '%m/%d/%Y %I:%M %p'))
    ) AS appt_minutes,
    -- e.g. "10/31/2026 2:30 PM" -> "Sat, Oct 31 at 2:30 PM"; NULL if the time can't be parsed.
    strftime(
        try_strptime(copy_of_appt_time, '%m/%d/%Y %I:%M %p'), 
        '%a, %b %-d at %-I:%M %p') AS appt_display,
    copy_of_shopping_cod as shopping_code,
    language,
    unique_field_merge
FROM submissions;
