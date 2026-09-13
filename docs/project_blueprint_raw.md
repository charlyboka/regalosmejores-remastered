This is the project for Regalosmejores (regalosmejores.com)

The idea is a website that generates as much traffic as possible and capitalizes via amazon affiliate links. 

The core of the application is an engine that takes user queries in natural language related to present search like "Regalos para el dia del padre" or "regalos para novios", or "que regalarle a nuevos padres". The key point is that it should take requests in natural language and wiht this be able suggest presents that are hihgly likely that would be interesting, with the goal that they would click the "see price" button for amazon and see them. 

Scope is spain although it should be designed in a way that in case it works, its easily scaled to any country where I canuse it 

This should be built with scalability and best software practices in mind. This is both at the level of creating tempaltes and reusable components for the for example cards for the items (could be of various formats with different attributes if needed, but the concpet of reusable components should be respected). Also for things like clients. 

Some components:
- Client for interacting with the Keepa API
- OpenAI client to use LLM, accepting system prompt, user prompt and model (default to gpt-5.6-luna given its cost effectivenetss). Potentially should also support embeddings if needed. If those models accept things liek temperature top p or other ways to modify the model those shoudl be accepted as parameters (if the model accepts it)


## Development approach and stack:
Heroku as the prefered way to host and deploy the app. I have purchades the domain www.regalosmejores.com in namecheap. Django as the main stack. Heroku postgres. UV for dependency management 

The main source of the products will be the Keepa API. I currently have a plan that gives me 1 req/minute although if needed i could purchase one that gives 10 per minute (can cummulate 60 credits)

An important component is the pipeline engine that should be responsible for populating the DB constantly and optimizing. Tere should be a complete pipeline system with potentially a base class or something and the capability to monitor them as I want to see which is the schedule for each pipeline, and a dashboard that should each run and the previous runs. 

Some pipelines:
- Generate present ideas -> This pipeline shoudl check high level what is in the DB already and generate a set of X present ideas that are not there and could be in some way original and complimentary. (note that i should also have the possibility of adding them manually myself). Ideas of present ideas should be user queries or things close to user queries like "regalos para el dia de la madre"
- Take a present idea (user query) and decompose it in possible searches in amazon that would yield good results for that. For example for "regalos para el dia del padre" would be good to get things like "fitbit", some options for kit to build a radio or smth like thin. if "presents for someone who lieks cycling" it should be something like "water bottles cycling", "bike portable repair kit" etc.
- Take a user query and register in the internal data model the asin with all the needed information. Basically populate an item with thigs like name, descriptiong, reviews and whatever other available item we would have from keepa api. Here is where possibly we could enrich an item with embeddings or some description via llm that would help find the appropiate ones when a user searches. 
(Later on) - some maintainance pipeline that would check if an item is still active or update. The idea is that the items get a date when they got added and if its too old then maybe they should be rescraped as maybe they could be decomissioned etc. 
The idea is that the pipelines are active 24/7 and are continously populating and maintainingg the DB. 

Those pipelines should manage properly the credit usage and have a proper prioritization or orchedstration. the idea is that the queues would be managed in a way that the backlog is cleared. A priori the  backlog to register items should be bigger than to process search queries and then finally the generate ideas. All those should run in certain schedule depending on the queue. The idea is also that if there is no keepa credits available you dont' unefficiently run the pipeline and waste genai tokens like for example to generate searches if we still ahve a backlog of products to register and waiting for token quota to repopulate. 

Main features:
- Genearl search -> This is the simple natural language query, easy to access. This could be embedded in various reusable components across the site. The idea if possible is that this search doesn't spend genai tokens (is there a way to somehow take the user query and match it with eitehr embeddings or some other similarity score to other already registered products?)
- Advanced search - Here is wehre you can add more questions/filters about for whom you can search the present. For example should be open field texts like "for whom is the present" "which occasion". Those shoudl be not many questions but shoudl help narrow a lot the pool of ideal presents and get an idea. They should all be optional. And here  maybe lets see if we can orchestrate a low cost llm call to get the result. (aprox cost with the cheapest luna model?)
- Blog with presents - Eventually we wil ahve a blog with lists of presents for different occastionss, type of persons etc, that would be a reusable template for articles of the kind "mejores regalos para padres en navidad" and many otehr topics. The idea is to eventually when the DB is very populated that I could have articles themed with popular search terms in a way that it would drive traffic and seo. thsi should be a list of cards witha  very small intro, with genai, and then a query run to pick up the best various items (thwy should be differentiated) that could be good suggestions. There should be a second type of article that would be "best" item, for example "best coffee machines for cold brew" or something like this that is if you want to give it as a present highlights pros and cons of various options.  We will implement this later as this require a base of articles already

To add:
- Tracking -> How can we track (and register) what users search, conversion rate, etc?


Responses:
- gpt-5.6-luna as the model, clarifies
- Keepa tier 20 tokens/min - i will use this one to have more population room. This way the budget will be more permissible
- user query -> the gift idea/theme. Search keywords in amazon is another thing
- No proices display. See price and see in amazon button. We want to have a lot of real estate. You can also add "see reviews" that ideally brings you to the reviews part in the amazon product desctiption page. 
- This site spanish only. the idea would be that I could easily reuse the skeleton etc to create quickly an analogous site for another country where amazon operates. 
- Traffic strategy - Prioritize organic content. I will see if I do some paid later
- Advance search is just more info, no logins or accounts/saved etc. 
- In the short term I will not have amazon creatosrs api. No prices displyaed, images ill take the risk. As soon as I have amazon api I will switch to it to be fully safe. 
- Ensure there is some human touch to ensure the site doesnt spam ai generated content in articles. At a later stage I will create a pipeline for articles but this will be later, and It will be max 1 per day. ideally at unpredictable different times and any other method to try to evade AI content detection. Again, this is later and mainly to bring organic traffic. 
- The data model looks good just make sure the product has when it has been fetched. Ensure the click event registers from which page it comes and other potnetially useful info (simple search, advance search, this would be something liek source...)
- Regarding the task runner - I am fine with queue and 1 concurrency as the pipelines are gonna be fast running. I let you pick which is the stack, just want to ensure its not overcomplicated, its resilient and as economic as possible. Remember I want to have a dashboard, made if needed, to monitor queue, executed pipelines etc (so might need those in the data model or so). I also want one to track clicks etc. Remeber for the topic list i also want to be able to add them manually
- Since I am going to pay for the big rate of the api, the token budget becomes less important. I would ike to still have somethign to gate it but its not going to be as scarce as I will have 20 tokens per minute. 
- I like some filters in the seeding strategy to get good products. Win win to get the most relevant ones and the ones that would convert better as this is waht evenue becomes eventually. 
- Follow the 3.6 recos in infra, I would like to have a plan for scalability and clear step by step of what I need to add or provision (like cloudflare)
- Search architecture looks good, embedding on search and generative work off pipelines, query query matching and not product matching. The pipelines and structure should tehrefore be mdoified to support this approach and to ensure that the presents are easy to match in various queries. The queries of the users should be recorded (!!!) and maybe there should be some internal system that remaps products to match certain queries if that is needed. Think about a data model that is compatible with growing queries and growing products and has some either self healing or updating. 
- The results of the presents should be a list of various matches, ensuring they are different topics. Organized in an appealing card way that has various buttons (see price, see reveiws, product details etc) to click the amaozn link. Those should be tracked. 
- Addance search suggestions sound good. 
- Make sure the user queries are registered and with this its easy to account for those and cover what people are actually looking for. I should be able to prioritize topics and being covered for such toopics. Also maybe when a query is indexed there should eb a mecanism for detachin near identical queries, we want maintainability and scalability.
- I like the idea for SEO to turn queries into indexable URLs, but wondering if this would be a problem for GenAI detection filters. Im wondering if it would be good to use it for example for the simple search and something differnet for the advanced search.
- Sitemap schema etc, robots.txt implement, all the things that would help SEO-wise. 
- Ensure amazon variations doesn't flood the site. At this point no A/B testing to make sure we don't create too many features at once. 
- Remember i will udpate the ties to 20 keepa tokens/min so limiter gets reduces, i will increase even more if needed. Not sure if the limit will be mroe than 60 so stil good to have maybe the gate or check before spending llm or failing pipelines but they will repplenish rapidly.

Questions
- Data model? What from keepa, what from other sources. What is the final data model?
- What about monetization options? Would love to get extra revenue with that, is exoic/mediavine ways to ge tthere? if so, any barrier of entry? otherwise its a no brainer
- If implementing first the simpel and the advanced, how to structure the site in a way that will attract as much organic traffic as possible. Other pages before having the blog etc that would bring traffic and not be gated by Ai generated content filters? I want to optimize that. 


Later:
- Adds serving?
- Where to advertise the site? 

