# review service

The review server stores reveiws.

You POST to /reviews and it makes a review. The body has app_id, user_id, and stars (1 thru 5).
You can also include a `text` field for the body of the review.

After posting it talks to catelog so the average updates.

There is also a /reviews/<app_id> GET that gives all reviews for that app, but pagintation isnt
done yet. moderation is not handled.
